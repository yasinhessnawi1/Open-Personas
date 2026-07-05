"""`RunEvent` — the SSE event stream for the run viewer (spec §8).

The :meth:`AgenticLoop.run` ``on_event`` callback receives :class:`RunEvent`
objects that the API (spec 08) serialises to SSE; each event type maps to a
visual element in the run viewer (spec 09). The loop never constructs a
``RunEvent`` by hand — it calls one of the typed classmethod constructors, which
are the single place each event's ``type`` string and ``data`` payload shape are
defined (DRY).

`RunEvent` is frozen Pydantic v2 (D-06-1): it crosses the spec-08 SSE
serialisation boundary. The ``data`` payload is ``dict[str, Any]`` so events can
carry structured detail (tool names, output text); the constructors are
responsible for building **JSON-safe** payloads (tool calls are rendered to
name/args dicts, never raw model objects) so ``model_dump_json`` always succeeds.
"""

from __future__ import annotations

from datetime import UTC, datetime  # noqa: TC003 — Pydantic needs runtime access
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from persona.schema.tools import ToolCall, ToolResult

    from persona_runtime.agentic.run import Run
    from persona_runtime.questions import QuestionOption

__all__ = ["RunEvent"]


class RunEvent(BaseModel):
    """One event in a run's lifecycle, serialised to SSE by the API (spec §8).

    Attributes:
        type: The event kind — one of ``started``, ``tier``, ``thinking``,
            ``memory_recall``, ``tool_calling``, ``tool_result``,
            ``activity_start``, ``activity_end`` (P2 — the unified "using <X>…"
            activity contract), ``asking_user``, ``user_responded``,
            ``reasoning``, ``completed``, ``cancelled``, ``max_steps``,
            ``error``, ``finished``.
        step: The zero-based step index the event belongs to (``-1`` for
            run-level events that precede the first step, e.g. ``started``).
        data: Event-type-specific JSON-safe payload built by the constructor.
        timestamp: tz-aware UTC time the event was emitted.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: str
    step: int
    data: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime

    @field_validator("timestamp", mode="after")
    @classmethod
    def _timestamp_must_be_tz_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            msg = "naive datetime not allowed on RunEvent.timestamp; use datetime.now(UTC)"
            raise ValueError(msg)
        return value.astimezone(UTC)

    # Section: typed constructors (the single place each payload shape lives)

    @classmethod
    def started(cls, task: str) -> RunEvent:
        """The run has begun executing ``task``."""
        return cls(type="started", step=-1, data={"task": task}, timestamp=datetime.now(UTC))

    @classmethod
    def tier(cls, tier: str, routing: dict[str, Any] | None = None) -> RunEvent:
        """The model tier chosen for this turn/step (run-level; ``step=-1``).

        Used by the chat SSE stream (``ConversationLoop.turn``) to surface the
        router's actual tier choice — and available to the run viewer too. One
        event vocabulary across both streams.

        Args:
            tier: the chosen tier name.
            routing: Spec 31 (D-31-1) — an optional, concise model-decision
                summary (``chosen_model`` + ``dominant_factor`` + the
                model-fallback flag/reason). Present only when intelligent
                model-within-tier selection ran this turn; absent ⇒ the
                pre-Spec-31 bare-tier payload (back-compat). The raw score
                vector is never on the wire — it stays in the JSONL TurnLog.
        """
        data: dict[str, Any] = {"tier": tier}
        if routing is not None:
            data["routing"] = routing
        return cls(type="tier", step=-1, data=data, timestamp=datetime.now(UTC))

    @classmethod
    def thinking(cls, step: int) -> RunEvent:
        """The model is generating the next action for ``step``."""
        return cls(type="thinking", step=step, data={}, timestamp=datetime.now(UTC))

    @classmethod
    def memory_recall(cls, step: int, store: str, count: int | None = None) -> RunEvent:
        """A typed-memory store was consulted while composing this turn/step.

        Spec 35 (D-35-4): the chat surface stages a "Recalling from <store>
        memory" state that *names* the store being recalled, with a
        store-coloured pulse — the typed-memory architecture made felt in
        context. One event per store consulted (≤4/turn, ordered).

        Emitted from the shared conditioning-retrieval path (``retrieve_context``)
        so the chat SSE stream and the run stream share one vocabulary. It is a
        dedicated type (not an overload of ``thinking``, which stays an
        empty-payload signal). Absent on streams whose retrieval passes no
        ``on_event`` callback — e.g. the voice turn (D-35-5), which reuses the
        same retrieval path but opts out of emitting.

        Args:
            step: The step index (``-1`` for the run-level chat turn, mirroring
                ``tier``; a real step index inside the agentic loop).
            store: The typed store consulted — one of ``identity`` /
                ``self_facts`` / ``worldview`` / ``episodic``.
            count: The number of chunks retrieved from the store, or ``None``
                when not reported (omitted from the payload).
        """
        data: dict[str, Any] = {"store": store}
        if count is not None:
            data["count"] = count
        return cls(type="memory_recall", step=step, data=data, timestamp=datetime.now(UTC))

    @classmethod
    def tool_calling(
        cls,
        step: int,
        tool_calls: list[ToolCall],
        *,
        kind_of: Callable[[str], str] | None = None,
    ) -> RunEvent:
        """The model requested tool dispatches this step (JSON-safe call list).

        Spec 30 T01 (D-30-1): when ``kind_of`` is provided, each call dict carries
        an additive ``kind`` (``builtin`` / ``skill`` / ``mcp:builtin`` /
        ``mcp:optional``) so the frontend can badge the call by source. Absent
        (``kind_of is None``) the payload is byte-identical to the pre-spec-30
        shape — the back-compat default (the ``produced_files`` precedent). The
        resolver is :meth:`persona.tools.Toolbox.kind_for`; passing it (rather
        than the kind values) keeps the single resolution site authoritative.
        """
        calls = [
            {
                "name": c.name,
                "call_id": c.call_id,
                "args": c.args,
                **({"kind": kind_of(c.name)} if kind_of is not None else {}),
            }
            for c in tool_calls
        ]
        names = ", ".join(c.name for c in tool_calls)
        return cls(
            type="tool_calling",
            step=step,
            data={"tool_names": names, "tool_calls": calls},
            timestamp=datetime.now(UTC),
        )

    @classmethod
    def tool_result(
        cls, step: int, tool_name: str, result: ToolResult, *, kind: str | None = None
    ) -> RunEvent:
        """A tool dispatch completed (success or ``is_error=True``).

        D-F4-X-event-kind-for-produced-files (Spec F4 Phase 5 T02b — Option A):
        forward structured ``produced_files`` from ``ToolResult.data`` onto
        the event payload when present. The sandbox tool factory at
        ``packages/core/src/persona/sandbox/tool.py:269-279`` populates
        ``result.data["produced_files"]`` as ``list[{path, size_bytes,
        media_type}]``; pre-amendment this constructor dropped it.

        Additive (back-compat): pre-existing frames lacked the field; the
        F4 frontend dispatcher reads it when present and falls back to a
        result-block render when absent. **One edit covers both chat SSE
        AND RunEvent transports** because this constructor is the single
        place each event's payload shape is defined (see module docstring
        lines 7-8) — chat ``_sse(ev.type, ev.data)`` (bare payload, D-09-1)
        and run ``model_dump_json(event)`` (envelope with ``.data`` nested)
        both observe the same upstream shape.

        Empty ``produced_files: []`` is omitted from the payload (absence
        IS the back-compat shape; renderers treat absence as "no files").
        """
        data: dict[str, Any] = {
            "tool_name": tool_name,
            "is_error": result.is_error,
            "content": result.content,
        }
        # Spec 30 T01 (D-30-1): additive source badge. Same single-site,
        # back-compat-when-absent shape as produced_files/artifacts below; covers
        # both chat SSE and RunEvent transports. Omitted when the caller does not
        # resolve a kind (pre-spec-30 frames).
        if kind is not None:
            data["kind"] = kind
        if result.data is not None:
            pf = result.data.get("produced_files")
            if isinstance(pf, list) and pf:
                data["produced_files"] = pf
        # Spec 28 — forward ToolResult.artifacts onto the payload so the web
        # renders an inline FileCard + right-panel renderer. Same single-site,
        # additive, empty-omitted shape as produced_files above (covers both
        # chat SSE bare-payload and RunEvent envelope transports). When present,
        # artifacts are the preferred render path; the frontend normaliser
        # falls back to produced_files only when artifacts is absent.
        if result.artifacts:
            data["artifacts"] = [a.model_dump() for a in result.artifacts]
        return cls(
            type="tool_result",
            step=step,
            data=data,
            timestamp=datetime.now(UTC),
        )

    @classmethod
    def activity_start(
        cls,
        step: int,
        *,
        activity_id: str,
        kind: str,
        name: str,
        label: str,
        args_summary: dict[str, str],
    ) -> RunEvent:
        """A capability is about to run — drives the live "using <X>…" state (P2).

        The pre-execution half of the unified activity contract (P2-D-3). Emitted
        by the runtime adapter for ``persona.tools.activity.ActivityObserver`` from the
        single dispatch boundary (``ObservedToolbox``), so every builtin tool / skill /
        MCP / sandbox / imagegen / web call signals before it runs — paired with an
        :meth:`activity_end` by ``activity_id``.

        Coexists additively with ``tool_calling``/``tool_result`` during the migration
        (P2-D-3 keep-both): the frontend renders the "using X…" affordance from this
        contract, not from ``tool_result`` (no double-render). ``args_summary`` is
        redacted at the core emit boundary (P2-D-2) — never carries secrets.

        Args:
            step: Step index (``-1`` for run-level chat/voice turns; a real step in
                the agentic loop), mirroring ``tool_result``.
            activity_id: Stable id pairing this start with its end.
            kind: Capability family (``tool``/``skill``/``mcp``/``sandbox``/
                ``imagegen``/``web``/``memory_recall``).
            name: The specific name (tool name, ``mcp:server:tool``, ``use_skill``).
            label: A human label for the affordance ("Searching the web").
            args_summary: Redacted, bounded ``dict[str, str]`` arg summary.
        """
        return cls(
            type="activity_start",
            step=step,
            data={
                "activity_id": activity_id,
                "kind": kind,
                "name": name,
                "label": label,
                "args_summary": args_summary,
            },
            timestamp=datetime.now(UTC),
        )

    @classmethod
    def activity_end(
        cls,
        step: int,
        *,
        activity_id: str,
        status: str,
        duration_ms: float,
        is_error: bool,
        result_summary: str = "",
    ) -> RunEvent:
        """A capability finished, was denied, or is awaiting approval (P2).

        The post-execution half of the activity contract, paired to its
        :meth:`activity_start` by ``activity_id``. ``status`` is one of ``ok`` /
        ``error`` / ``denied`` / ``awaiting_approval`` — the last two come from A3's
        deny/gate at the same boundary (P2-D-a3-composition); a returned
        ``is_error`` result maps to ``error`` (never silently ``ok``).

        Carries the lifecycle/display essentials only; the rich-output payload
        (full content, ``produced_files``, ``artifacts``) stays on the coexisting
        ``tool_result`` during the migration (P2-D-3 keep-both) — moving it onto this
        event is the deferred ``tool_result``-retirement step, avoiding a double-persist
        of artifacts while both events emit.

        Args:
            step: Step index (mirrors the paired :meth:`activity_start`).
            activity_id: Pairs this end with its start.
            status: Terminal outcome (``ok``/``error``/``denied``/``awaiting_approval``).
            duration_ms: Wall-clock duration (monotonic), measured at the boundary.
            is_error: True on the model-recoverable failure surface.
            result_summary: A short, bounded outcome summary (omitted when empty).
        """
        data: dict[str, Any] = {
            "activity_id": activity_id,
            "status": status,
            "duration_ms": duration_ms,
            "is_error": is_error,
        }
        if result_summary:
            data["result_summary"] = result_summary
        return cls(
            type="activity_end",
            step=step,
            data=data,
            timestamp=datetime.now(UTC),
        )

    @classmethod
    def asking_user(
        cls,
        step: int,
        question: str,
        *,
        options: Sequence[QuestionOption] | None = None,
        allow_free_form: bool = True,
        proposal: Mapping[str, str] | None = None,
    ) -> RunEvent:
        """The persona asked the user a question.

        Spec 21 (D-21-9): additively carries the 3+1 proactive-question shape.
        When ``options`` is ``None`` (the model-initiated ``[ASK_USER]`` path and
        every pre-spec-21 frame) the payload is the bare ``{"question": ...}`` —
        byte-identical to the original shape, so existing renderers and the
        web ``AskingUserData`` type are unaffected. When ``options`` is present
        the payload adds the predefined options + free-form flag and the web
        renders the 3-button + free-form UI (T12). Absence IS the back-compat
        shape — exactly the ``produced_files`` precedent above.

        Args:
            step: The step index the question belongs to.
            question: The question text.
            options: The 3 predefined options, or ``None`` for a free-text ask.
            allow_free_form: Whether a free-form answer is accepted (only
                meaningful, and only emitted, when ``options`` is present).
        """
        data: dict[str, Any] = {"question": question}
        if options is not None:
            data["options"] = [{"label": o.label, "description": o.description} for o in options]
            data["allow_free_form"] = allow_free_form
        # Spec 30 (D-30-2): the general chat-proactive-question rail descriptor.
        # Additive — absent on a plain clarifying ask (the Spec-21 path); present
        # on a capability-gap offer so the web wires accept → grant/assign →
        # retry. The LOCKED {kind, name, provider?, action} envelope Spec 31 reuses.
        if proposal is not None:
            data["proposal"] = dict(proposal)
        return cls(type="asking_user", step=step, data=data, timestamp=datetime.now(UTC))

    @classmethod
    def user_responded(cls, step: int) -> RunEvent:
        """The user's answer was received and folded into context."""
        return cls(type="user_responded", step=step, data={}, timestamp=datetime.now(UTC))

    @classmethod
    def reasoning(cls, step: int, content: str) -> RunEvent:
        """Intermediate reasoning text (neither tool call, question, nor final)."""
        return cls(
            type="reasoning", step=step, data={"content": content}, timestamp=datetime.now(UTC)
        )

    @classmethod
    def completed(cls, step: int, output: str) -> RunEvent:
        """The model produced the final deliverable (``[FINAL]``)."""
        return cls(
            type="completed", step=step, data={"output": output}, timestamp=datetime.now(UTC)
        )

    @classmethod
    def cancelled(cls, step: int) -> RunEvent:
        """The run was cancelled at this step boundary."""
        return cls(type="cancelled", step=step, data={}, timestamp=datetime.now(UTC))

    @classmethod
    def max_steps(cls, step: int, summary: str) -> RunEvent:
        """The step budget was exhausted; ``summary`` is the best-effort output."""
        return cls(
            type="max_steps", step=step, data={"summary": summary}, timestamp=datetime.now(UTC)
        )

    @classmethod
    def error(cls, step: int, message: str) -> RunEvent:
        """An unrecoverable error terminated the run."""
        return cls(type="error", step=step, data={"message": message}, timestamp=datetime.now(UTC))

    @classmethod
    def finished(cls, run: Run) -> RunEvent:
        """The run is fully done (terminal); carries the final status + run id."""
        return cls(
            type="finished",
            step=len(run.steps),
            data={"run_id": run.id, "status": str(run.status)},
            timestamp=datetime.now(UTC),
        )

    @classmethod
    def task_originated(
        cls,
        *,
        owner_id: str,
        persona_id: str,
        persona_name: str,
        conversation_id: str,
        assistant_message_id: str,
        contract: Mapping[str, Any],
        schedule: Mapping[str, Any],
        draft_hash: str,
    ) -> RunEvent:
        """A confirmed standing-task contract, ready for the api to create (Spec A4, A4-D-X).

        Emitted on the confirm turn; the chat-turn worker consumes it and calls the api
        ``OriginationService`` to create the A2 task + A1 schedule + A3 matrix. The event is
        **data only** — it crosses the runtime→api boundary as a JSON payload, so creation
        lands in api while recognition/echo/confirm stay in runtime (runtime ⊥ api is
        structural, not conventional). ``assistant_message_id`` is the primary idempotency
        anchor (one proposal turn = one task); ``draft_hash`` is the content-dedup fallback.

        Args:
            owner_id: The tenant the task runs as (RLS scope).
            persona_id: The persona that will execute the task.
            persona_name: The persona's display name (for the persona-voiced failure account).
            conversation_id: The originating conversation.
            assistant_message_id: The confirm turn's stable id — the primary dedup key.
            contract: ``Contract.model_dump(mode="json")`` (goal/scope/criteria/bounds/policy).
            schedule: The parsed cadence ``{recurrence|one_time_at, timezone}`` (JSON-safe).
            draft_hash: A stable content hash of the canonical draft (the dedup fallback).
        """
        return cls(
            type="task_originated",
            step=-1,
            data={
                "owner_id": owner_id,
                "persona_id": persona_id,
                "persona_name": persona_name,
                "conversation_id": conversation_id,
                "assistant_message_id": assistant_message_id,
                "contract": dict(contract),
                "schedule": dict(schedule),
                "draft_hash": draft_hash,
            },
            timestamp=datetime.now(UTC),
        )

    @classmethod
    def task_steering(cls, *, verb: str, task_id: str) -> RunEvent:
        """A conversational steering request — pause / resume / cancel a live task (Spec A4, T9b).

        Emitted by the runtime when the user steers a running task in chat; the chat-turn worker
        consumes it and applies the verb via the owner-scoped ``TaskStore`` (it injects ``owner_id``
        from its handle — the runtime knows the verb + task, the worker owns the tenant + mutation).
        Data only, so runtime ⊥ api holds.
        """
        return cls(
            type="task_steering",
            step=-1,
            data={"verb": verb, "task_id": task_id},
            timestamp=datetime.now(UTC),
        )

    @classmethod
    def initiative_verb(cls, *, verb: str, notice_id: str | None = None) -> RunEvent:
        """A conversational initiative verb — dial set / proposal confirm / decline (Spec A5, T10).

        Emitted by the runtime's initiative-verb gate; the chat-turn worker consumes it and
        applies the verb via the owner-scoped initiative service (dial write + schedule ensure,
        or the ledger-anchored proposal resolution — the reload-durable confirmation surface,
        the T9 Option-C consolidation). ``notice_id`` rides only on confirm/decline (the pending
        proposal the LEDGER identified — never conversation metadata). Data only; runtime ⊥ api.
        """
        data: dict[str, Any] = {"verb": verb}
        if notice_id is not None:
            data["notice_id"] = notice_id
        return cls(
            type="initiative_verb",
            step=-1,
            data=data,
            timestamp=datetime.now(UTC),
        )

    @classmethod
    def task_rescheduled(
        cls,
        *,
        task_id: str,
        timezone: str,
        recurrence_rrule: str | None = None,
        one_time_at: str | None = None,
        skip_next: bool = False,
    ) -> RunEvent:
        """A user-confirmed conversational reschedule — retime/rerule a live task (Spec A8, T6).

        Emitted by the runtime AFTER the user confirms the re-echoed new schedule clause; the
        chat-turn worker consumes it and applies the change through the CAS-guarded reschedule
        door (``actor=user_via_chat``), injecting ``owner_id`` from its handle. Carries the new
        cadence (an RRULE candidate XOR a one-time ISO instant) OR ``skip_next``, plus the tz the
        cadence is anchored in. Data only, so runtime ⊥ api holds.
        """
        return cls(
            type="task_rescheduled",
            step=-1,
            data={
                "task_id": task_id,
                "timezone": timezone,
                "recurrence_rrule": recurrence_rrule,
                "one_time_at": one_time_at,
                "skip_next": skip_next,
            },
            timestamp=datetime.now(UTC),
        )
