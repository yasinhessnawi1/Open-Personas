"""The model-backed checkpoint distiller (Spec W1, T13; D-W1-17, D-W1-18, D-W1-19).

The deterministic writer is honest and cheap and cannot think. It appends the leg's output to
the conclusions, folds the oldest into a marker when the budget runs short, and writes an
EMPTY ``next_step``, because a writer that cannot reason cannot invent a next action and the
one time it tried (R9-103) it handed the successor a finished answer as an instruction and
the task looped for hours producing the same paragraph.

So a task carries findings forward and never a plan. Every leg re-plans from the contract,
which is the ossification half of the twin D-A2-1 names: the run keeps its knowledge and
loses its intent.

This writer reads the leg the way a person would: the run's steps, what the prior checkpoint
held, and what the contract asked for, and it returns merged conclusions, the lessons worth
keeping, a plan, one concrete next step, and the open questions. One model call on the small
tier (D-W1-17: distillation is boilerplate, it runs once per leg inside the drain margin, so
latency matters and the eval decides whether small is enough).

Three properties hold it to the floor:

- **The deterministic writer is the fallback, always.** A timeout, a refusal, a malformed
  answer, a model that is simply down: every one of them delegates, so a leg's checkpoint is
  never lost to the distiller being clever. The floor is what production had before this.
- **The same budget.** The model's output is compacted through exactly the compaction the
  deterministic writer uses, so a verbose distillation cannot overflow the store's gate.
- **R9-103 stays fixed.** ``next_step`` is validated against the leg's own output: a model
  that echoes the answer back as the instruction gets an empty next step, the same as before.
  The loop that bug caused is not reopened by the writer that was supposed to close it.

Off by default (D-W1-18, ``PERSONA_TASK_SEMANTIC_DISTILLER_ENABLED``) until the continuation
eval's external judge run is green and recorded with its judge version.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from persona.logging import get_logger
from persona.schema.conversation import ConversationMessage
from persona.tasks import (
    DEFAULT_CHECKPOINT_TOKEN_BUDGET,
    TaskCheckpoint,
    checkpoint_token_count,
    merge_artifact_pointers,
)

from persona_runtime.legs.distiller import CompactingCheckpointWriter
from persona_runtime.legs.ledger import (
    LEDGER_TOKEN_SHARE,
    artifacts_from_run,
    fold_oldest,
    queries_from_run,
    sources_from_run,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from persona.backends import ChatBackend
    from persona.tasks import Task

    from persona_runtime.agentic.run import Run

__all__ = [
    "DEFAULT_DISTILL_TIMEOUT_S",
    "DISTILLER_PROMPT_VERSION",
    "SemanticCheckpointWriter",
]

_log = get_logger("legs.semantic_distiller")

#: Bump on any wording change to the prompt below, so an eval result names the prompt it
#: judged (the Spec 10 discipline the K3 guidance artifact follows).
DISTILLER_PROMPT_VERSION: Final = "w1-distiller-v1"

#: How long the distillation may take before the leg falls back to the deterministic writer.
#: The checkpoint write happens after the run, inside the worker's drain margin, so this is
#: the one number that decides whether a slow small tier costs a leg its plan.
#:
#: 45 seconds, from the W1 operator pass rather than from taste: the first real scheduled leg
#: timed out at 20 (the floor wrote, as designed, so the leg was fine and the plan was lost),
#: and the same distillation measured 5.5s and 15.2s standalone on the small tier that leg
#: used. A reasoning model thinks before it answers, and a leg's prompt is larger than a
#: standalone one. 45 leaves room for that and still lands inside the drain margin: a leg that
#: used its whole 180s wall clock plus this is 225s against a 270s drain.
DEFAULT_DISTILL_TIMEOUT_S: Final = 45.0

#: The most steps of the run summarised into the prompt. A leg is bounded at ten steps
#: today; the cap is here so a future wider box cannot turn one distillation into a
#: many-thousand-token call.
_MAX_STEPS_RENDERED: Final = 12

#: How much of one step's text reaches the prompt. Enough to say what the step did.
_MAX_STEP_CHARS: Final = 600

_MAX_TOKENS: Final = 800

_SYSTEM_PROMPT = """\
You are the memory of an autonomous agent working a long task in short legs, days apart. \
A leg has just finished. Everything it learned is lost unless you write it down, and the \
next leg will see ONLY what you write plus the task contract.

Read the leg's work and return the task's state after it.

Rules:
- CONCLUSIONS are what is established, not what happened. Merge the new leg's findings into \
the prior ones; drop anything superseded; never write a narrative of the steps.
- A conclusion that is still true stays, in its own words. Do not re-word settled findings \
for the sake of it; the next leg should recognise them.
- LESSONS are wrong turns worth not repeating. Few, short, and only if real.
- PLAN is what remains, in order. NEXT_STEP is the single concrete action the next leg \
starts with: an instruction, never an answer. If the task is finished, leave next_step empty.
- OPEN_QUESTIONS are things only the user can settle. Leave the list empty if there are none.
- Write for a reader who has never seen this conversation.

Reply with ONLY a JSON object, no prose:
{"conclusions": ["..."], "lessons": ["..."], "plan": ["..."], "next_step": "...", \
"open_questions": ["..."]}
"""

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


class SemanticCheckpointWriter:
    """A :class:`CheckpointWriter` that thinks, over a floor that does not (Spec W1, T13).

    Args:
        backend_provider: Resolves the small-tier backend at write time (the worker binds
            the owner scope per job, so the backend cannot be resolved once at build time).
            Returning ``None`` is the honest "no model here" and delegates to the floor.
        fallback: The deterministic writer every failure path delegates to. Defaults to a
            :class:`CompactingCheckpointWriter` on the same budget.
        token_budget: The checkpoint's accumulating-core budget, the same number the store
            gates on.
        timeout_s: How long the distillation may take before the floor takes over.
    """

    def __init__(
        self,
        *,
        backend_provider: Callable[[], ChatBackend | None],
        fallback: CompactingCheckpointWriter | None = None,
        token_budget: int = DEFAULT_CHECKPOINT_TOKEN_BUDGET,
        timeout_s: float = DEFAULT_DISTILL_TIMEOUT_S,
    ) -> None:
        self._backend_provider = backend_provider
        self._fallback = fallback or CompactingCheckpointWriter(token_budget=token_budget)
        self._budget = token_budget
        self._target = int(token_budget * 0.8)
        self._ledger_target = int(token_budget * LEDGER_TOKEN_SHARE)
        self._timeout_s = timeout_s

    async def write(
        self,
        *,
        task: Task,
        prior: TaskCheckpoint | None,
        run: Run,
        leg_id: str,
        seq: int,
        now: datetime,
    ) -> TaskCheckpoint:
        """Distil the leg into the next checkpoint, or hand the floor the job."""
        distilled = await self._distil(task=task, prior=prior, run=run)
        if distilled is not None:
            candidate = self._build(
                task=task,
                prior=prior,
                run=run,
                leg_id=leg_id,
                seq=seq,
                now=now,
                distilled=distilled,
            )
            # A distillation that cannot fit the store's gate is not a distillation. Folding
            # keeps whole entries (D-A2-1: compact, never truncate mid-thought), so a single
            # conclusion longer than the budget survives folding and would be REJECTED at the
            # append, which costs the leg its whole checkpoint: the W1 operator pass watched
            # exactly that park a task that had just done real work. The floor's write always
            # fits, because what it carries is the run's own bounded output.
            if checkpoint_token_count(candidate) <= self._budget:
                return candidate
            _log.info(
                "distillation exceeds the checkpoint budget task_id={tid}; the floor writes",
                tid=task.id,
            )
        return await self._fallback.write(
            task=task, prior=prior, run=run, leg_id=leg_id, seq=seq, now=now
        )

    async def _distil(
        self, *, task: Task, prior: TaskCheckpoint | None, run: Run
    ) -> dict[str, object] | None:
        """One model call, or ``None`` for every way it can fail to produce an answer."""
        backend = self._backend_provider()
        if backend is None:
            return None
        prompt = _render_prompt(task=task, prior=prior, run=run)
        asked_at = datetime.now(UTC)
        try:
            response = await asyncio.wait_for(
                backend.chat(
                    [
                        ConversationMessage(
                            role="system", content=_SYSTEM_PROMPT, created_at=asked_at
                        ),
                        ConversationMessage(role="user", content=prompt, created_at=asked_at),
                    ],
                    max_tokens=_MAX_TOKENS,
                ),
                timeout=self._timeout_s,
            )
        except TimeoutError:
            _log.info(
                "semantic distillation timed out task_id={tid}; the floor writes", tid=task.id
            )
            return None
        except Exception as exc:  # noqa: BLE001 - a checkpoint is never lost to this call
            _log.info("semantic distillation failed task_id={tid}: {err}", tid=task.id, err=exc)
            return None
        return _parse(response.content)

    def _build(
        self,
        *,
        task: Task,
        prior: TaskCheckpoint | None,
        run: Run,
        leg_id: str,
        seq: int,
        now: datetime,
        distilled: dict[str, object],
    ) -> TaskCheckpoint:
        """Assemble the checkpoint, bounded by exactly the floor's compaction."""
        conclusions = _strings(distilled.get("conclusions"))
        if not conclusions:
            # A distillation with nothing established is not a distillation. Keep what the
            # prior checkpoint had rather than writing the run's findings out of existence.
            conclusions = list(prior.progress_conclusions) if prior is not None else []
            if run.output:
                conclusions.append(run.output)
        queries = fold_oldest(
            _merge(prior.queries_run if prior is not None else (), queries_from_run(run)),
            target_tokens=self._ledger_target,
            noun="queries",
        )
        sources = fold_oldest(
            _merge(prior.sources_seen if prior is not None else (), sources_from_run(run)),
            target_tokens=self._ledger_target,
            noun="sources",
        )
        lessons = _strings(distilled.get("lessons"))
        spent = _tokens(queries) + _tokens(sources) + _tokens(lessons)
        return TaskCheckpoint(
            task_id=task.id,
            leg_id=leg_id,
            checkpoint_seq=seq,
            progress_conclusions=tuple(_fold_conclusions(conclusions, target=self._target - spent)),
            lessons=tuple(lessons),
            current_plan=tuple(_strings(distilled.get("plan"))),
            next_step=_safe_next_step(distilled.get("next_step"), run=run),
            open_questions=tuple(_strings(distilled.get("open_questions"))),
            queries_run=queries,
            sources_seen=sources,
            # R9-162: the leg's real files, not an empty tuple copied forward. Deliberately
            # NOT from the model's answer: a path is a fact about what was persisted, and a
            # distiller that could invent one would point the next leg at a file that is not
            # there. Read from the run's own ``ToolResult.artifacts`` either way.
            artifact_pointers=merge_artifact_pointers(
                prior.artifact_pointers if prior is not None else (), artifacts_from_run(run)
            ),
            event_log_cursor=run.id,
            updated_at=now,
        )


def _safe_next_step(raw: object, *, run: Run) -> str:
    """The next action, unless it is the leg's own answer wearing an instruction's clothes.

    R9-103: a ``next_step`` that echoes the leg's output is handed to the successor as
    ``NEXT STEP: ...``, which it reads as its instruction, so it re-derives the same answer
    and writes it back as the next ``next_step``. A recurring task emitted byte-identical
    output every hour and paid full price each time. The deterministic writer solved it by
    writing nothing; a writer that can produce a real next step still must not produce THAT
    one, so the echo is refused here rather than trusted to the prompt.
    """
    step = str(raw or "").strip()
    if not step:
        return ""
    output = (run.output or "").strip()
    if not output:
        return step
    return "" if _is_echo(step, output) else step


def _is_echo(step: str, output: str) -> bool:
    """Is this "next step" just the answer again?

    Verbatim equality catches the exact case R9-103 hit. The containment checks catch the
    near misses that behave identically: the answer with a sentence bolted on the front, or
    the first paragraph of it. Normalised on whitespace and case, because a model that
    reformats the same text has still handed back the same text.
    """
    a, b = _normalise(step), _normalise(output)
    if not a or not b:
        return False
    if a == b:
        return True
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return len(shorter) >= 80 and shorter in longer


def _normalise(text: str) -> str:
    return " ".join(text.split()).casefold()


def _render_prompt(*, task: Task, prior: TaskCheckpoint | None, run: Run) -> str:
    """What the distiller is shown: the contract, the prior state, and the leg's work."""
    lines = [f"CONTRACT GOAL: {task.contract.goal}"]
    if task.contract.scope:
        lines.append(f"SCOPE: {task.contract.scope}")
    lines.append(f"DELIVERABLE: {task.contract.deliverable.render()}")
    if prior is not None:
        if prior.progress_conclusions:
            lines.append("\nPRIOR CONCLUSIONS:")
            lines.extend(f"- {c}" for c in prior.progress_conclusions)
        if prior.current_plan:
            lines.append("\nPRIOR PLAN:")
            lines.extend(f"- {step}" for step in prior.current_plan)
        if prior.open_questions:
            lines.append("\nPRIOR OPEN QUESTIONS:")
            lines.extend(f"- {q}" for q in prior.open_questions)
    lines.append("\nTHIS LEG'S WORK:")
    lines.extend(_render_steps(run))
    if run.output:
        lines.append(f"\nTHIS LEG'S OUTPUT:\n{run.output[: _MAX_STEP_CHARS * 2]}")
    lines.append(f"\nTHIS LEG ENDED: {run.status}")
    return "\n".join(lines)


def _render_steps(run: Run) -> list[str]:
    """The leg's steps in the terms the distiller reasons about: what it asked, what came back."""
    rendered: list[str] = []
    for index, step in enumerate(run.steps[:_MAX_STEPS_RENDERED]):
        for call in step.tool_calls:
            rendered.append(f"- step {index}: called {call.name} {_short(str(call.args))}")
        for result in step.results:
            verb = "failed" if result.is_error else "returned"
            rendered.append(f"  {result.tool_name} {verb}: {_short(result.content)}")
        if step.content and not step.tool_calls:
            rendered.append(f"- step {index}: {_short(step.content)}")
    if len(run.steps) > _MAX_STEPS_RENDERED:
        rendered.append(f"- ({len(run.steps) - _MAX_STEPS_RENDERED} further steps not shown)")
    return rendered or ["- (the leg ran no steps)"]


def _short(text: str) -> str:
    collapsed = " ".join(text.split())
    return collapsed[:_MAX_STEP_CHARS]


def _parse(content: str) -> dict[str, object] | None:
    """The model's JSON, or ``None`` so the floor takes over.

    A distiller that cannot be parsed has told us nothing, and guessing at half-parsed
    output is how a checkpoint ends up holding a fragment of a sentence forever.
    """
    stripped = _FENCE_RE.sub("", content or "").strip()
    match = re.search(r"\{.*\}", stripped, re.DOTALL)
    if match is None:
        return None
    try:
        parsed = json.loads(match.group(0))
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _strings(raw: object) -> list[str]:
    """A list of non-empty strings, whatever shape the model reached for."""
    if isinstance(raw, str):
        return [raw.strip()] if raw.strip() else []
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def _merge(prior: Sequence[str], fresh: Sequence[str]) -> list[str]:
    seen: dict[str, None] = {}
    for entry in (*prior, *fresh):
        seen.setdefault(entry, None)
    return list(seen)


def _tokens(entries: Sequence[str]) -> int:
    from persona.skills import count_tokens

    return count_tokens(" ".join(entries))


def _fold_conclusions(conclusions: Sequence[str], *, target: int) -> list[str]:
    """Exactly the floor's compaction, so a wordy distillation cannot overflow the gate."""
    from persona.skills import count_tokens

    kept = list(conclusions)
    if count_tokens(" ".join(kept)) <= target:
        return kept
    compacted = 0
    while len(kept) > 1 and count_tokens(" ".join(kept)) > target:
        kept.pop(0)
        compacted += 1
    if compacted:
        return [f"[{compacted} earlier findings compacted, see run records]", *kept]
    return kept
