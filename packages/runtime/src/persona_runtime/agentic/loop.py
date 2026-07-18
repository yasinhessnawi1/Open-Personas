"""The AgenticLoop — the plan-act-reflect keystone (spec §4; T06).

One ``run(task)`` executes a task to completion via the simplest possible agent
loop (architecture §5.2): a ``for step in range(max_steps)`` where one model call
per step is classified — ``tool_call`` / ``ask_user`` / ``final`` / ``reasoning``
— tools are dispatched and fed back, errors are surfaced for the *model* to
recover from, the step history is compacted near the tier budget, and the run
terminates on a final marker, cancellation, or the step cap. No planner, no DAG,
no multi-agent orchestration.

The decisions that shape this file:

- **D-06-2 (status-based termination):** the loop NEVER raises ``MaxStepsReached``
  / ``RunCancelled`` — every terminal outcome returns a ``Run`` with the right
  ``RunStatus``. A best-effort max-steps summary is an *output*, never a
  ``completed`` signal.
- **D-06-5 (loop-appends the agentic framing):** the persona block, the task, and
  the ``[ASK_USER]``/``[FINAL]`` marker instructions are merged into ONE floor
  system message (``context[0]``) — the compactor's invariant. ``PromptBuilder``
  stays chat-focused.
- **D-06-6 (step-tier policy in the loop):** ``_tier_for_step`` grades the tier
  (planning/final → frontier, tool continuation → mid); ``force_frontier_tier``
  is the spec §11 marker-mitigation escape hatch. The chat ``Router`` is untouched.
- **D-06-7 (no inner tool-round cap):** a step is exactly one ``chat()`` call; all
  the tool calls it requests are dispatched and fed into the next step. ``max_steps``
  is the only budget.
- **D-06-8 (episodic write-back tags a skill candidate):** the end-of-run episodic
  chunk carries ``source=agentic_run`` + run/task/tools/steps/status metadata.
- **D-06-9 (reuse, don't subclass):** ``format_tool_result``, the ``use_skill``
  intercept (D-04-10), and the D-05-X compaction idiom are reused; the top-level
  loop is owned.
- **D-06-10 (``user_respond`` is an awaited callback):** the loop ``await``\\ s it;
  spec 08 owns the blocking wiring.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from persona.audit import AuditAction
from persona.autonomy import policy_for, resolve_autonomy
from persona.errors import SkillCompositionDepthError, SkillCycleError
from persona.logging import get_logger
from persona.schema.chunks import ChunkProvenance, PersonaChunk, WriteSource, mint_chunk_id
from persona.schema.conversation import ConversationMessage
from persona.schema.tools import ToolResult
from persona.skills import (
    DenyUnvettedConsent,
    collect_skill_supplements,
    count_tokens,
    default_nonce,
    injection_consent_state,
    render_skill_index,
    self_framed,
    skill_audit_event,
)
from persona.skills.composition import AdmissionResult, SkillCompositionState
from persona.tools import format_tool_result

from persona_runtime.agentic.compactor import StepHistoryCompactor
from persona_runtime.agentic.events import RunEvent
from persona_runtime.agentic.run import CancelToken, Run, RunStatus, StepUsage
from persona_runtime.agentic.step import Step, StepType
from persona_runtime.errors import TierNotConfiguredError
from persona_runtime.prompt import RetrievedContext
from persona_runtime.question_author import TemplateQuestionAuthor
from persona_runtime.questions import QuestionRegistry
from persona_runtime.routing import tier_for
from persona_runtime.safety_intercept import InterceptAction, classify_user_message

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from persona.audit import AuditLogger
    from persona.backends import ChatBackend, ChatResponse
    from persona.sandbox.result import SandboxFile
    from persona.schema.persona import Persona
    from persona.schema.skills import SkillSpec
    from persona.schema.tools import ToolCall
    from persona.skills import SkillConsentPort, SkillInjector
    from persona.stores.protocol import MemoryStore
    from persona.tools import Toolbox

    from persona_runtime.crisis_encoder import CrisisScorer
    from persona_runtime.prompt import PromptBuilder

    # Spec 18 T06: type-hint widened to the Router Protocol; behaviour
    # untouched per D-18-X-agentic-loop-routing-coupling (step-tier is
    # task-decomposition driven via _tier_for_step, NOT consulted via
    # Router.route). The chat ``Router`` is untouched at this seam.
    from persona_runtime.question_author import QuestionAuthor
    from persona_runtime.routing import Router
    from persona_runtime.tier import TierRegistry

__all__ = ["AgenticLoop"]

_logger = get_logger("agentic.loop")

_RETRIEVE_TOP_K = 3
_DEFAULT_MAX_TOKENS = 4096
_ASK_USER_MARKER = "[ASK_USER]"
#: The typed-store recall order for the P2 memory_recall bridge (mirrors
#: ``persona_runtime.retrieval._RECALL_ORDER``); identity / self_facts / worldview /
#: episodic are the four stores ``_retrieve`` consults for a run's initial context.
_RECALL_ORDER = ("identity", "self_facts", "worldview", "episodic")
_FINAL_MARKER = "[FINAL]"
_NO_CALLBACK_REPLY = "Please proceed with your best judgment."
_HALLUCINATION_NUDGE = "You must use only the tools listed as available. Do not invent tool names."
# A reasoning step (text-only — no tool call, no marker) ends the context on an
# assistant message. The next chat() call would then end on an assistant
# message, which Anthropic (and any provider without assistant-prefill) rejects
# with a 400 ("the conversation must end with a user message"). Appending this
# user-role continuation BOTH fixes the 400 AND pushes the plan-act-reflect loop
# forward: it tells the model to take a concrete next action (call a tool) or
# emit [FINAL] if done. The wording echoes the marker-floor instructions
# (_AGENTIC_INSTRUCTIONS) so the marker contract stays consistent.
_REASONING_CONTINUE_NUDGE = (
    "Continue the task: take the next concrete action by calling one of the "
    f"available tools, or if the task is complete, give your final deliverable "
    f"prefixed with {_FINAL_MARKER}."
)
_AGENTIC_INSTRUCTIONS = (
    "You are completing a task end to end. Plan before acting, and use the tools "
    "available to you rather than guessing. When you need information from the user "
    f"to proceed, ask exactly one question prefixed with {_ASK_USER_MARKER}. When the "
    f"task is complete, give your final deliverable prefixed with {_FINAL_MARKER}."
)
_SUMMARISE_INSTRUCTION = (
    "Summarise the following step history from an in-progress task into a short "
    "paragraph, preserving what was searched, fetched, decided, and produced. Be concise."
)
# A short, tool-call-free response containing a question mark is treated as an
# implicit ask-user (the heuristic fallback when the model omits the marker).
_ASK_USER_HEURISTIC_MAX_CHARS = 300


def _backend_max_tokens(backend: ChatBackend) -> int:
    """Best-effort context budget (mirrors the conversation loop's helper)."""
    value = getattr(backend, "max_tokens", None)
    return value if isinstance(value, int) and value > 0 else _DEFAULT_MAX_TOKENS


class AgenticLoop:
    """Runs a task to completion via plan-act-reflect (spec §4).

    Pure dependency injection (engineering standards §1.2). The loop owns no
    state beyond its collaborators; it produces a serialisable :class:`Run` and
    emits :class:`RunEvent`\\ s. It never persists the ``Run`` (spec 08 does) and
    never owns the :class:`TierRegistry` lifecycle (D-05-4).
    """

    def __init__(
        self,
        *,
        persona: Persona,
        stores: dict[str, MemoryStore],
        toolbox: Toolbox,
        skill_injector: SkillInjector,
        scanned_skills: list[SkillSpec],
        prompt_builder: PromptBuilder,
        router: Router,
        tier_registry: TierRegistry,
        compactor: StepHistoryCompactor | None = None,
        max_steps: int = 20,
        force_frontier_tier: bool = False,
        question_author: QuestionAuthor | None = None,
        nonce_source: Callable[[], str] | None = None,
        skill_consent: SkillConsentPort | None = None,
        audit_logger: AuditLogger | None = None,
        crisis_encoder: CrisisScorer | None = None,
    ) -> None:
        self._persona = persona
        self._stores = stores
        self._toolbox = toolbox
        # R6 (R6-D-3): the app-scoped crisis encoder (RuntimeFactory-wired). ``None`` is
        # the additive lexical-only path — byte-identical to V11. Composed inside
        # ``classify_user_message``; fail-soft→R0 lives there.
        self._crisis_encoder = crisis_encoder
        # Spec 21 T07: proactive-question scaffold + caps (D-21-9/5/6). The
        # author wraps the model's [ASK_USER] question with 3+1 options; the
        # per-run cap + dedup registry are reset per run() (mirrors the
        # deferred_input_files per-run reset — one run per loop instance).
        self._question_author = question_author or TemplateQuestionAuthor()
        self._question_registry = QuestionRegistry()
        self._questions_asked = 0
        self._question_cap = 0
        self._injector = skill_injector
        self._scanned_skills = scanned_skills
        self._skills_by_name = {s.name: s for s in scanned_skills}
        # Spec S1 (S1-D-2): per-injection nonce source for the subordination
        # guard; defaults to production ``default_nonce`` (tests pin it).
        self._nonce_source = nonce_source or default_nonce
        # Spec S1 (S1-D-6/D-7): enable-time consent gate (default DENY-unvetted)
        # + the optional injection audit sink (``None`` ⇒ no-op).
        self._skill_consent: SkillConsentPort = skill_consent or DenyUnvettedConsent()
        self._audit = audit_logger
        self._builder = prompt_builder
        self._router = router  # reserved for chat-style routing; step-tier is _tier_for_step
        self._tiers = tier_registry
        self._compactor = compactor or StepHistoryCompactor()
        self._max_steps = max_steps
        self._force_frontier = force_frontier_tier
        # M1a per-step deferred input_files (D-16-2, D-16-2-state-location).
        # Mutated by the use_skill intercept; drained by the composition
        # root's deferred_input_files_provider callable wired into the
        # code_execution factory. Cleared at every run() entry.
        self.deferred_input_files: list[SandboxFile] = []
        # Spec 24 (D-24-4): per-run skill-composition chain; re-created at each
        # run() entry (declared here so the attribute is always present).
        self._composition = SkillCompositionState(budget=self._injector.TOKEN_BUDGET)

    async def run(
        self,
        task: str,
        on_event: Callable[[RunEvent], Awaitable[None]] | None = None,
        user_respond: Callable[[str], Awaitable[str]] | None = None,
        cancel_token: CancelToken | None = None,
        on_step_usage: Callable[[StepUsage], Awaitable[None]] | None = None,
    ) -> Run:
        """Execute ``task`` to completion, returning the final :class:`Run`.

        Emits events via ``on_event`` (the API serialises them to SSE). Blocks on
        ``user_respond`` when the model asks a question (D-06-10). Respects
        ``cancel_token`` at each step boundary (D-06-7; never mid-step).

        Args:
            task: The task to execute.
            on_event: Optional async callback for each :class:`RunEvent`.
            user_respond: Optional async callback the loop ``await``\\ s on an
                ask-user step; ``None`` → the loop proceeds with best judgment.
            cancel_token: Optional caller-held cancellation control.
            on_step_usage: Optional async billing callback (Spec M3, T4a) — invoked
                with a :class:`StepUsage` right after each step's model call so the
                caller can meter the step's real cost incrementally and flip
                ``cancel_token`` to cut the run off at the NEXT step boundary on
                exhaustion. NOT an SSE event (stays off the client stream);
                ``None`` → no metering (byte-unchanged for non-billing callers).

        Returns:
            The :class:`Run` with all steps, the final status, and the output.
        """
        persona_id = self._require_persona_id()
        started_at = datetime.now(UTC)
        self.deferred_input_files.clear()  # M1a per-run reset (D-16-2)

        # R1-hard out-of-band bypass (Spec V11, V11-D-5) — the same gate as the chat
        # loop and the voice path. An acute, explicit W1 task takes the persona (and
        # the lock that suppresses crisis-noticing) out of the loop entirely: emit the
        # deterministic safe completion as the run output with NO steps, never running
        # the agentic loop. Agentic output is text (no TTS), so the chat rendering is
        # used. One acute path: bypass.
        safety_verdict = classify_user_message(
            task,
            locale=self._persona.identity.language_default,
            encoder=self._crisis_encoder,
        )
        if safety_verdict.action is InterceptAction.HARD and safety_verdict.completion is not None:
            await self._emit(on_event, RunEvent.started(task))
            run = Run(
                persona_id=persona_id,
                task=task,
                status=RunStatus.COMPLETED,
                steps=[],
                output=safety_verdict.completion.chat_text,
                error=None,
                started_at=started_at,
                finished_at=datetime.now(UTC),
            )
            self._write_episodic_summary(persona_id, run)
            await self._emit(on_event, RunEvent.finished(run))
            return run
        # Spec 24 (D-24-4): per-run skill-composition chain (depth cap + cycle
        # detection + shared budget). Reset each run, like deferred_input_files.
        self._composition = SkillCompositionState(budget=self._injector.TOKEN_BUDGET)
        # Spec 21 T07: per-run proactive-question state. Cap is autonomy-scaled
        # (D-21-5: cautious 5 / balanced 3 / decisive 1); resolved from the
        # self_facts learning chain overlay (D-21-8/11). Questions consume a
        # step regardless (D-21-15).
        self._question_registry = QuestionRegistry()
        self._questions_asked = 0
        level = resolve_autonomy(
            self._persona,
            self._stores["self_facts"].get_all(persona_id, include_superseded=True),
        )
        self._question_cap = policy_for(level).questions_per_run
        run_id = ""  # set once we build the Run below
        steps: list[Step] = []
        status = RunStatus.RUNNING
        output: str | None = None
        error: str | None = None

        # `started` first (the run began), then build the initial context — which
        # emits the P2 memory_recall events for the stores it consults (so the recall
        # signals follow `started`, mirroring the chat turn's ordering).
        await self._emit(on_event, RunEvent.started(task))
        context = await self._build_initial_context(persona_id, task, on_event)

        last_bad_tool: str | None = None  # for the hallucinated-twice escalation (§5.2)

        for step_num in range(self._max_steps):
            if cancel_token is not None and cancel_token.is_cancelled:
                status = RunStatus.CANCELLED
                await self._emit(on_event, RunEvent.cancelled(step_num))
                break

            tier = self._tier_for_step(step_num, steps[-1].type if steps else None)
            backend = self._tiers.get(tier)

            await self._emit(on_event, RunEvent.thinking(step_num))
            step_started = time.perf_counter()
            response = await backend.chat(context, tools=self._toolbox.get_specs())
            latency_ms = (time.perf_counter() - step_started) * 1000.0
            tokens = response.usage.total_tokens
            # Spec M3 (T4a): surface this step's real model-call usage for
            # incremental billing. The caller meters + may flip ``cancel_token``;
            # the next iteration's boundary check (top of the loop) then stops the
            # run cleanly — the in-flight step below completes first.
            if on_step_usage is not None:
                await on_step_usage(
                    StepUsage(
                        step=step_num,
                        provider=backend.provider_name,
                        model=backend.model_name,
                        prompt_tokens=response.usage.prompt_tokens,
                        completion_tokens=response.usage.completion_tokens,
                        cost_usd=response.usage.cost_usd,
                    )
                )

            if response.tool_calls:
                step, last_bad_tool, context = await self._handle_tool_calls(
                    step_num,
                    response,
                    backend,
                    context,
                    tier,
                    tokens,
                    latency_ms,
                    last_bad_tool,
                    on_event,
                )
                steps.append(step)
            elif self._is_ask_user(response):
                step, context = await self._handle_ask_user(
                    step_num,
                    response,
                    context,
                    tier,
                    tokens,
                    latency_ms,
                    user_respond,
                    on_event,
                )
                steps.append(step)
            elif self._is_final(response):
                output = self._strip_marker(response.content, _FINAL_MARKER)
                steps.append(
                    Step(
                        type=StepType.FINAL,
                        content=output,
                        tier_used=tier,
                        tokens=tokens,
                        latency_ms=latency_ms,
                    )
                )
                status = RunStatus.COMPLETED
                await self._emit(on_event, RunEvent.completed(step_num, output))
                break
            else:
                # Reasoning step: append the assistant text, then a user-role
                # continuation nudge so the context ends user-role before the
                # next chat() (Bug fix — no assistant-prefill 400) and the loop
                # actually progresses toward a tool call or [FINAL].
                context = [
                    *context,
                    self._assistant(response.content),
                    self._user(_REASONING_CONTINUE_NUDGE),
                ]
                steps.append(
                    Step(
                        type=StepType.REASONING,
                        content=response.content,
                        tier_used=tier,
                        tokens=tokens,
                        latency_ms=latency_ms,
                    )
                )
                await self._emit(on_event, RunEvent.reasoning(step_num, response.content))

            context = await self._maybe_compact(context, backend)
        else:
            status = RunStatus.MAX_STEPS_REACHED
            output = await self._best_effort_summary(context)
            await self._emit(on_event, RunEvent.max_steps(self._max_steps, output))

        run = Run(
            persona_id=persona_id,
            task=task,
            status=status,
            steps=steps,
            output=output,
            error=error,
            started_at=started_at,
            finished_at=datetime.now(UTC),
        )
        run_id = run.id
        _logger.info("agentic run finished run_id={rid} status={st}", rid=run_id, st=str(status))
        self._write_episodic_summary(persona_id, run)
        await self._emit(on_event, RunEvent.finished(run))
        return run

    # ----- step handlers ---------------------------------------------------

    async def _handle_tool_calls(
        self,
        step_num: int,
        response: ChatResponse,
        backend: ChatBackend,
        context: list[ConversationMessage],
        tier: str,
        tokens: int,
        latency_ms: float,
        last_bad_tool: str | None,
        on_event: Callable[[RunEvent], Awaitable[None]] | None,
    ) -> tuple[Step, str | None, list[ConversationMessage]]:
        """Dispatch every tool call this step requested; feed results back (§5.1/§5.2).

        No inner cap (D-06-7): all calls dispatch, then the loop advances. A
        not-allowed / not-registered tool yields ``ToolResult(is_error=True, ...)``
        (D-03-3) the model recovers from; the same bad name twice in a row adds a
        stronger instruction (§5.2).
        """
        await self._emit(
            on_event,
            RunEvent.tool_calling(
                step_num, list(response.tool_calls), kind_of=self._toolbox.kind_for
            ),
        )
        new_context = list(context)
        if backend.supports_native_tools:
            # Native providers require the assistant's tool_calls to precede the
            # tool results (spec 11 soak finding); carry any narration as content.
            new_context.append(
                self._assistant_with_tool_calls(response.content, list(response.tool_calls))
            )
        elif response.content:
            new_context.append(self._assistant(response.content))

        results: list[ToolResult] = []
        bad_tool_this_step: str | None = None
        for call in response.tool_calls:
            result = await self._dispatch(call, step_num=step_num, on_event=on_event)
            results.append(result)
            if result.is_error and self._is_unknown_tool(call):
                bad_tool_this_step = call.name
            new_context.append(
                format_tool_result(call, result, provider_name=backend.provider_name)
            )
            await self._emit(
                on_event,
                RunEvent.tool_result(
                    step_num, call.name, result, kind=self._toolbox.kind_for(call.name)
                ),
            )

        # Inject any activated skills AFTER all tool results, so the tool-result
        # messages stay contiguous (backends expect them grouped) rather than a
        # skill-injection system message landing between two tool results.
        for call, result in zip(response.tool_calls, results, strict=True):
            await self._maybe_inject_skill(call, result, new_context)

        # Hallucinated-twice escalation (§5.2).
        if bad_tool_this_step is not None and bad_tool_this_step == last_bad_tool:
            new_context.append(self._system(_HALLUCINATION_NUDGE))

        step = Step(
            type=StepType.TOOL_CALL,
            tool_calls=list(response.tool_calls),
            results=results,
            tier_used=tier,
            tokens=tokens,
            latency_ms=latency_ms,
        )
        return step, bad_tool_this_step, new_context

    async def _handle_ask_user(
        self,
        step_num: int,
        response: ChatResponse,
        context: list[ConversationMessage],
        tier: str,
        tokens: int,
        latency_ms: float,
        user_respond: Callable[[str], Awaitable[str]] | None,
        on_event: Callable[[RunEvent], Awaitable[None]] | None,
    ) -> tuple[Step, list[ConversationMessage]]:
        """Ask the user a question and fold their answer back into context (§4.2).

        Spec 21 T07: the question carries 3+1 options (D-21-9), and is bounded by
        the autonomy-scaled per-run cap (D-21-5) + per-run dedup (D-21-6). When
        the cap is exhausted or the question repeats, the loop does NOT pause —
        it proceeds with best judgment (the D-21-18 stated-assumption analogue) —
        but still records an ``ASK_USER`` step (a question consumes a step,
        D-21-15). The model-initiated ``[ASK_USER]`` marker path is preserved.
        """
        question = self._strip_marker(response.content, _ASK_USER_MARKER)
        suppressed = self._questions_asked >= self._question_cap or self._question_registry.seen(
            question
        )
        if suppressed:
            _logger.info(
                "agentic proactive question suppressed asked={n} cap={cap}",
                n=self._questions_asked,
                cap=self._question_cap,
            )
            new_context = [
                *context,
                self._assistant(response.content),
                self._user(_NO_CALLBACK_REPLY),
            ]
            return (
                Step(
                    type=StepType.ASK_USER,
                    question=question,
                    user_answer=None,
                    tier_used=tier,
                    tokens=tokens,
                    latency_ms=latency_ms,
                ),
                new_context,
            )

        pq = await self._question_author.default(
            question, language=self._persona.identity.language_default
        )
        await self._emit(
            on_event,
            RunEvent.asking_user(
                step_num, question, options=pq.options, allow_free_form=pq.allow_free_form
            ),
        )
        self._questions_asked += 1
        self._question_registry.record(question)
        new_context = [*context, self._assistant(response.content)]
        answer: str | None = None
        if user_respond is not None:
            answer = await user_respond(question)
            new_context.append(self._user(answer))
            await self._emit(on_event, RunEvent.user_responded(step_num))
            self._question_registry.record(question, answer)
        else:
            new_context.append(self._user(_NO_CALLBACK_REPLY))
        step = Step(
            type=StepType.ASK_USER,
            question=question,
            user_answer=answer,
            tier_used=tier,
            tokens=tokens,
            latency_ms=latency_ms,
        )
        return step, new_context

    # ----- classification (§4.2; markers primary, heuristic fallback) ------

    def _is_final(self, response: ChatResponse) -> bool:
        return _FINAL_MARKER in response.content

    def _is_ask_user(self, response: ChatResponse) -> bool:
        if _ASK_USER_MARKER in response.content:
            return True
        # Heuristic fallback (D-06-5 / steer #5): a short, tool-call-free response
        # ending in a question. No classifier.
        text = response.content.strip()
        return bool(text) and "?" in text and len(text) <= _ASK_USER_HEURISTIC_MAX_CHARS

    @staticmethod
    def _strip_marker(content: str, marker: str) -> str:
        return content.replace(marker, "").strip()

    # ----- tier policy (D-06-6) --------------------------------------------

    def _tier_for_step(self, step_num: int, last_action: StepType | None) -> str:
        """The tier for this step — the agentic_step policy surface (P9-D-1).

        Spec P9 supersedes the D-06-6 step-grading (first/planning → frontier,
        tool-continuation → mid): every agentic step produces USER-READ run
        output, so all steps route through ``tier_for("agentic_step")``
        (frontier — the Phase 1 gate ruling). ``force_frontier_tier`` (the
        spec §11 marker-mitigation) is retained and now coincides with the
        policy default. ``step_num``/``last_action`` stay in the signature so
        a future non-user-read step class can re-grade without a caller change.
        """
        if self._force_frontier:
            return "frontier"
        del step_num, last_action  # P9: every step is user-read — one surface
        return tier_for("agentic_step")

    # ----- compaction bridge (D-06-4; the loop owns the async pre-compute) --

    async def _maybe_compact(
        self, context: list[ConversationMessage], backend: ChatBackend
    ) -> list[ConversationMessage]:
        """Compact the step history if it crosses the tier budget (§6, D-06-4).

        The loop predicts compaction, pre-computes the small-tier summary on the
        middle slice (the one ``await``), and hands the compactor the resolved
        string. Never ``asyncio.run()`` in a sync callable.
        """
        budget = _backend_max_tokens(backend)
        if not self._compactor.should_compact(context, budget):
            return context
        middle = self._compactor.middle_to_summarise(context)
        if not middle:
            return context
        summary = await self._summarise(middle)
        return self._compactor.compact_if_needed(context, budget, summary=summary)

    async def _summarise(self, messages: list[ConversationMessage]) -> str:
        """Summarise an excerpt on the background tier (the one async summary call).

        Spec P9: internal compaction — unread — the background surface (small).
        """
        backend = self._tiers.get(tier_for("background"))
        rendered = "\n".join(f"{m.role}: {m.content}" for m in messages)
        prompt = [self._system(_SUMMARISE_INSTRUCTION), self._user(rendered)]
        response = await backend.chat(prompt)
        return response.content.strip()

    async def _best_effort_summary(self, context: list[ConversationMessage]) -> str:
        """Best-effort summary at max-steps, generated on the frontier tier (§4.1)."""
        try:
            backend = self._tiers.get("frontier")
        except TierNotConfiguredError:
            backend = self._tiers.get("mid")
        rendered = "\n".join(f"{m.role}: {m.content}" for m in context)
        prompt = [
            self._system(
                "The task did not complete within the step budget. Summarise what was "
                "accomplished and what remains, as a best-effort partial result."
            ),
            self._user(rendered),
        ]
        response = await backend.chat(prompt)
        return response.content.strip()

    # ----- use_skill intercept (D-04-10 / D-06-9) --------------------------

    def _emit_skill_audit(self, spec: SkillSpec, action: AuditAction, consent_state: str) -> None:
        """Emit exactly one skill AuditEvent (S1-D-7); ``None`` sink ⇒ no-op, inert."""
        if self._audit is None:
            return
        try:
            self._audit.emit(
                skill_audit_event(
                    persona_id=self._persona.persona_id or "",
                    spec=spec,
                    action=action,
                    consent_state=consent_state,
                    timestamp=datetime.now(UTC),
                )
            )
        except Exception:  # noqa: BLE001 — audit must never break the run
            _logger.warning("skill audit emit failed skill={s}", s=spec.name)

    def _consent_ok(self, spec: SkillSpec) -> bool:
        """Whether ``spec`` may be injected under the enable-time consent gate (S1-D-6)."""
        if not spec.trust.requires_consent:
            return True
        content_hash = spec.provenance.content_hash if spec.provenance else ""
        return self._skill_consent.is_enabled(
            self._persona.persona_id or "", spec.name, content_hash or ""
        )

    async def _maybe_inject_skill(
        self,
        call: ToolCall,
        result: ToolResult,
        context: list[ConversationMessage],
    ) -> None:
        """If a ``use_skill`` call succeeded, inject the skill content into context.

        Spec 24 (D-24-4): activations are subject to the per-run composition
        chain — depth cap, cycle detection, shared budget. Cycle/depth refusals
        and budget skips append an informative system message instead (the run
        proceeds); the first skill goes through the per-skill injector, a
        composed skill (already shown to fit) is appended verbatim.

        Spec S1 (S1-D-6/D-7): the enable-time consent gate runs FIRST — an
        above-threshold skill without granted consent is refused (audited
        ``SKILL_REFUSED``, the run proceeds) and never touches the budget. A
        successful injection is audited ``SKILL_INJECTED``.
        """
        if call.name != "use_skill" or result.data is None or "skill_name" not in result.data:
            return
        name = str(result.data["skill_name"])
        spec = self._skills_by_name.get(name)
        if spec is None:
            return
        if not self._consent_ok(spec):
            self._emit_skill_audit(spec, AuditAction.SKILL_REFUSED, "refused_no_consent")
            _logger.info("skill activation refused: no consent skill={s}", s=name)
            context.append(
                self._system(
                    f"Skill '{name}' was not activated: it is an untrusted skill that has "
                    "not been approved for this persona."
                )
            )
            return
        try:
            admission = self._composition.admit(name, content_tokens=spec.content_token_count)
        except (SkillCycleError, SkillCompositionDepthError) as exc:
            _logger.info("skill composition refused reason={r}", r=type(exc).__name__)
            context.append(self._system(f"Skill '{name}' was not activated: {exc}."))
            return
        if admission is AdmissionResult.SKIPPED_BUDGET:
            context.append(
                self._system(
                    f"Skill '{name}' was not activated: the skill budget is exhausted. "
                    "Continue with the instructions already loaded."
                )
            )
            return
        content = (
            await self._injector.inject(spec) if self._composition.depth == 1 else spec.content
        )
        self._composition.record_injected(count_tokens(content))
        self.deferred_input_files.extend(collect_skill_supplements(spec))
        # Spec S1 (S1-D-2, the headline hole-closer): the agentic loop appends
        # activated-skill content as a SYSTEM-role message — which would hand raw
        # skill content system authority ABOVE the subordination. We append it
        # SELF-FRAMED instead: the authority preamble + the nonce-delimited
        # envelope ride inline (there is no single governing preamble for an
        # appended message), so the skill content is explicitly demoted to
        # advisory-subordinate even while it rides a system message. Without this
        # the guard is silently bypassed on the agentic path.
        guarded = self_framed(content, trust=spec.trust, nonce=self._nonce_source())
        self._emit_skill_audit(
            spec, AuditAction.SKILL_INJECTED, injection_consent_state(spec.trust)
        )
        context.append(self._system(f"Activated skill '{name}'.\n\n{guarded}"))

    # ----- dispatch + error recovery (§5.1/§5.2) ---------------------------

    async def _dispatch(
        self,
        call: ToolCall,
        *,
        step_num: int = -1,
        on_event: Callable[[RunEvent], Awaitable[None]] | None = None,
    ) -> ToolResult:
        """Dispatch a tool call, converting structural failures to is_error results.

        A not-allowed / not-registered tool raises (spec 03); we convert to
        ``ToolResult(is_error=True, content=...)`` so the model can recover
        (D-03-3 — ``tool_name`` is required on the synthesised result). A tool
        that runs but fails already returns ``is_error=True`` and is returned
        unchanged.

        P2: dispatch routes through :func:`dispatch_with_activity` so each call emits a
        paired ``activity_start``/``activity_end`` at ``step_num`` when ``on_event`` is
        present (the live "using <X>…" state + trail). The existing ``tool_result`` keeps
        emitting additively during the migration (P2-D-3 keep-both).
        """
        from persona.errors import ToolExecutionError, ToolNotAllowedError
        from persona.schema.tools import truncated_tool_call_message

        # Lazy import: persona_runtime.activity imports persona_runtime.agentic.events,
        # which pulls persona_runtime.agentic.__init__ → this module; a top-level import
        # would be a partially-initialised cycle. Imported here (like the errors above).
        from persona_runtime.activity import dispatch_with_activity

        # The provider truncated the call mid-JSON (finish_reason="length" or
        # unparseable arguments). Dispatching with empty args yields the cryptic
        # "Field required" and an identical-retry loop; return actionable
        # guidance so the model shortens/splits instead.
        if call.truncated:
            return ToolResult(
                tool_name=call.name,
                call_id=call.call_id,
                is_error=True,
                content=truncated_tool_call_message(call.name),
            )

        try:
            return await dispatch_with_activity(
                self._toolbox, call, on_event=on_event, step=step_num
            )
        except ToolNotAllowedError:
            available = ", ".join(self._toolbox.names())
            return ToolResult(
                tool_name=call.name,
                call_id=call.call_id,
                is_error=True,
                content=f"Tool '{call.name}' is not available. Available tools: {available}",
            )
        except ToolExecutionError as exc:
            return ToolResult(
                tool_name=call.name,
                call_id=call.call_id,
                is_error=True,
                content=f"Tool '{call.name}' failed: {exc}",
            )

    def _is_unknown_tool(self, call: ToolCall) -> bool:
        return not self._toolbox.is_allowed(call.name)

    # ----- episodic write-back (D-06-8; the approved skill-candidate tag) --

    def _write_episodic_summary(self, persona_id: str, run: Run) -> None:
        """Write one combined episodic chunk tagging the run as a skill candidate.

        D-06-8: the metadata lets a future spec 13 find agentic-run entries
        without a retroactive migration. ``PersonaChunk.metadata`` is
        ``dict[str, str]`` — ``tools_used`` is a comma-joined sorted set, ``steps``
        is stringified.
        """
        store = self._stores["episodic"]
        # Minted uuidv7 id (K8-D-6) — the former store-count index was an
        # O(N) read per write and raced under concurrent writers.
        chunk_id = mint_chunk_id(persona_id, "episodic")
        now = datetime.now(UTC)
        tools_used = sorted({call.name for step in run.steps for call in step.tool_calls})
        store.write(
            persona_id,
            [
                PersonaChunk(
                    id=chunk_id,
                    text=f"TASK: {run.task}\n\nOUTCOME: {run.output or '(no output)'}",
                    metadata={
                        "source": "agentic_run",
                        "run_id": run.id,
                        "task": run.task,
                        "tools_used": ",".join(tools_used),
                        "steps": str(len(run.steps)),
                        "status": str(run.status),
                    },
                    created_at=now,
                    provenance=ChunkProvenance(
                        source=WriteSource.SYSTEM,
                        logical_id=chunk_id,
                        version=1,
                        written_at=now,
                        written_by="agentic.run",
                    ),
                ),
            ],
            source=WriteSource.SYSTEM,
            written_by="agentic.run",
        )

    # ----- context construction --------------------------------------------

    async def _build_initial_context(
        self,
        persona_id: str,
        task: str,
        on_event: Callable[[RunEvent], Awaitable[None]] | None = None,
    ) -> list[ConversationMessage]:
        """Build the floor: ONE system message (persona block + agentic framing) + task.

        The persona prompt is assembled by the reused ``PromptBuilder`` (D-06-9);
        the agentic instructions are appended into the same floor system message
        so ``context[0]`` is the compactor's whole invariant (D-06-5). The task is
        the trailing user message.

        P2 (memory_recall bridge): runs now emit a ``memory_recall`` event per typed
        store consulted for the initial context — parity with the chat loop
        (``loop.py``), which was the only surface emitting it (Spec 35 D-35-4). Emitted
        at ``step=-1`` (run-level, mirroring the chat turn) since the recall feeds the
        run's whole initial context, before the first step.
        """
        retrieved = self._retrieve(persona_id, task)
        if on_event is not None:
            for store in _RECALL_ORDER:
                await on_event(RunEvent.memory_recall(-1, store, len(getattr(retrieved, store))))
        skill_index = render_skill_index(self._scanned_skills)
        built = self._builder.build(
            self._persona,
            retrieved,
            [],
            skill_index,
            task,
            max_tokens=_DEFAULT_MAX_TOKENS,
        )
        # built == [system, task_user]; merge the agentic instructions into the
        # system message so the floor is a single context[0].
        system = built[0]
        floor = ConversationMessage(
            role="system",
            content=f"{system.content}\n\n{_AGENTIC_INSTRUCTIONS}\n\nTASK: {task}",
            created_at=datetime.now(UTC),
        )
        return [floor]

    def _retrieve(self, persona_id: str, task: str) -> RetrievedContext:
        identity = self._stores["identity"].get_all(persona_id)
        self_facts = self._stores["self_facts"].query(persona_id, task, _RETRIEVE_TOP_K)
        worldview = self._stores["worldview"].query(persona_id, task, _RETRIEVE_TOP_K)
        episodic = self._stores["episodic"].query(persona_id, task, _RETRIEVE_TOP_K)
        return RetrievedContext(
            identity=identity,
            self_facts=self_facts,
            worldview=worldview,
            episodic=episodic,
        )

    # ----- small helpers ----------------------------------------------------

    @staticmethod
    def _system(text: str) -> ConversationMessage:
        return ConversationMessage(role="system", content=text, created_at=datetime.now(UTC))

    @staticmethod
    def _user(text: str) -> ConversationMessage:
        return ConversationMessage(role="user", content=text, created_at=datetime.now(UTC))

    @staticmethod
    def _assistant(text: str) -> ConversationMessage:
        return ConversationMessage(role="assistant", content=text, created_at=datetime.now(UTC))

    @staticmethod
    def _assistant_with_tool_calls(text: str, calls: list[ToolCall]) -> ConversationMessage:
        """The assistant message that issued tool_calls — required to precede the
        tool results for native providers (spec 11 soak finding)."""
        return ConversationMessage(
            role="assistant", content=text, created_at=datetime.now(UTC), tool_calls=calls
        )

    async def _emit(
        self,
        on_event: Callable[[RunEvent], Awaitable[None]] | None,
        event: RunEvent,
    ) -> None:
        if on_event is not None:
            await on_event(event)

    def _require_persona_id(self) -> str:
        pid = self._persona.persona_id
        if pid is None:
            msg = "persona_id is required for the agentic loop"
            raise ValueError(msg)
        return pid
