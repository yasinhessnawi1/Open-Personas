"""Door (b)'s concrete pieces — the small-tier candidate producer (layer b) + the deterministic
wellbeing subject-exclusion (layer a) that the handler seam enforces (Spec A7, T8; criterion 8).

Two layers close criterion 8 — *gated-category content in an event never becomes an unprompted
initiative subject via the event door* — mirroring A5's criterion-6 defence-in-depth exactly:

- **Layer (a) — deterministic, at the HANDLER seam** (:class:`ApiEventWellbeingCheck`): a grounding
  is gated iff any wellbeing-flagged node was *sourced from* that grounding (a flagged node whose
  provenance ``interaction_id`` equals the grounding ref — the same ``flagged_nodes`` set the
  pipeline's own subject rule reads, so it does not shrink the real residual). Enforced by
  :class:`~persona_api.events.candidate_handler.EventCandidateHandler` BEFORE any producer runs —
  structural for every door-b candidate ever, not producer discretion (the impossible-green
  doctrine). A drop is audited (``event_trigger.candidate_wellbeing_dropped``), never silent.
- **Layer (b) — the model raise-nothing drop, here** (:class:`SmallTierEventCandidateProducer`): the
  scanner's own-drop analogue — the small-tier prompt is instructed to surface NOTHING for
  wellbeing-sensitive content.

The residual is the same as A5's own: content that has never been ingested into the graph carries no
flag yet, so layer (a) cannot see it and only the model drop (b) guards it — the accepted A5 posture
(a fresh sensitive fact not yet tagged relies on the scanner's own drop there too).

The producer grounds on the FIXED citation the dispatcher already chose (A7-D-7: ``conversation``
for a message, ``task`` for a lifecycle event) — the model scores the judgment (observation,
trigger, plan, value/acceptance), never the refs, so there is no fabrication surface. Fail-soft:
any resolution/model/parse failure returns ``None`` (a thin event is success, like a thin scan).
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from persona.initiative import (
    CandidateSource,
    CitationKind,
    GroundingCitation,
    InitiativeCandidate,
    InitiativeTrigger,
    PlannedStep,
    Urgency,
)
from persona.logging import get_logger
from persona.schema.conversation import ConversationMessage
from persona.tools.categories import ActionCategory
from pydantic import ValidationError

if TYPE_CHECKING:
    from persona.backends.protocol import ChatBackend
    from persona.graph.protocol import GraphStore
    from persona.initiative import InitiativeSettings
    from persona.jobs import JobContext

    from persona_api.events.candidate_handler import EventCandidatePayload

__all__ = [
    "EVENT_CANDIDATE_PROMPT_VERSION",
    "ApiEventWellbeingCheck",
    "GroundingContentSource",
    "SmallTierEventCandidateProducer",
]

_log = get_logger("api.events.candidate_producer")

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
_CONTENT_SNIPPET = 2000  # the citable grounding tail handed to the model (bounded like the scan)

#: Bumped on any rule/example change; stamped on every produced candidate's ``prompt_version`` so a
#: behaviour change is traceable (the A5 INITIATIVE_SCAN_PROMPT_VERSION discipline).
EVENT_CANDIDATE_PROMPT_VERSION = "a7-event-candidate-v1"


@runtime_checkable
class GroundingContentSource(Protocol):
    """Resolve the citable content behind a grounding ref (the A5 ``ApiGroundingSource``)."""

    def conversation_content(self, owner_id: str, conversation_id: str) -> str | None: ...

    def task_content(self, owner_id: str, task_id: str) -> str | None: ...


class ApiEventWellbeingCheck:
    """Layer (a): the deterministic wellbeing subject-exclusion over the graph flag set.

    A grounding is gated iff a wellbeing-flagged node was *sourced from* it — i.e. some
    :meth:`~persona.graph.protocol.GraphStore.flagged_nodes` node carries a provenance entry whose
    ``interaction_id`` equals the grounding ref. In practice this fires for ``conversation``
    grounding (an inbound message that ingestion has already tagged as sensitive); ``task``
    grounding never names an interaction, so lifecycle candidates fall to the model drop (layer b) —
    the honest residual (a task's goal/conclusions are not the sensitive-disclosure vector criterion
    8 targets). Reuses the SAME flagged set the pipeline's subject rule reads, so it does not shrink
    the real residual (why option B — a parallel gate — was rejected).
    """

    def __init__(self, store: GraphStore) -> None:
        self._store = store

    def is_gated_subject(self, owner_id: str, grounding_kind: str, grounding_ref: str) -> bool:  # noqa: ARG002 — kind reserved; the ref match is kind-agnostic and defensive
        """``True`` iff a wellbeing-flagged node was sourced from this grounding (drop the door)."""
        for node in self._store.flagged_nodes(owner_id):
            if any(p.interaction_id == grounding_ref for p in node.provenance):
                return True
        return False


class SmallTierEventCandidateProducer:
    """Layer (b): one event's grounding → a scored ``source=EVENT`` candidate, or ``None``.

    The event analogue of :class:`~persona_runtime.initiative.scan.InitiativeScanner`: it resolves
    the FIXED grounding the dispatcher chose, runs a single small-tier call scoring the judgment,
    and stamps ``source=EVENT``. ``None`` means raise nothing (an ungroundable, unwelcome, or
    wellbeing-sensitive event is success — silence, like a thin scan). Never raises.
    """

    def __init__(
        self,
        *,
        backend: ChatBackend,
        grounding: GroundingContentSource,
        settings: InitiativeSettings,
    ) -> None:
        self._backend = backend
        self._grounding = grounding
        self._settings = settings

    async def produce(
        self, payload: EventCandidatePayload, context: JobContext
    ) -> InitiativeCandidate | None:
        """Score the event; every failure degrades to ``None`` (silence is the safe state)."""
        owner = context.owner_id
        content = self._resolve(owner, payload.grounding_kind, payload.grounding_ref)
        if not content:
            # Ungroundable at read time (a vanished conversation/task) — nothing to notice, no call.
            return None
        try:
            response = await self._backend.chat(
                self._messages(payload, content),
                temperature=0.0,
                max_tokens=900,
            )
        except Exception:  # noqa: BLE001 — a model failure is silence, never a crash (A5 fail-soft)
            _log.warning("event candidate model call failed; producing nothing (fail-soft)")
            return None
        return self._parse(response.content, payload, owner=owner)

    # --- input ----------------------------------------------------------------

    def _resolve(self, owner_id: str, kind: str, ref: str) -> str | None:
        if kind == CitationKind.CONVERSATION.value:
            return self._grounding.conversation_content(owner_id, ref)
        if kind == CitationKind.TASK.value:
            return self._grounding.task_content(owner_id, ref)
        return None

    def _messages(self, payload: EventCandidatePayload, content: str) -> list[ConversationMessage]:
        now = datetime.now(UTC)
        body = (
            f"An event fired a standing watch: {payload.human}.\n\n"
            f"The material this event grounds on:\n{content[:_CONTENT_SNIPPET]}\n\n"
            "Reply with the JSON object."
        )
        return [
            ConversationMessage(
                role="system", content=EVENT_CANDIDATE_SYSTEM_PROMPT, created_at=now
            ),
            ConversationMessage(role="user", content=body, created_at=now),
        ]

    # --- output ---------------------------------------------------------------

    def _parse(
        self, text: str, payload: EventCandidatePayload, *, owner: str
    ) -> InitiativeCandidate | None:
        raw = self._load_json(text)
        if raw is None:
            _log.warning("event candidate output unparseable; producing nothing (fail-soft)")
            return None
        item = raw.get("candidate")
        if not isinstance(item, dict):
            # ``{"candidate": null}`` is the first-class NOTHING outcome (restraint is the product).
            return None
        # The grounding is FIXED by the dispatcher (A7-D-7) — the model scores the judgment, never
        # the ref, so a fabricated citation is structurally impossible here.
        citation = GroundingCitation(
            kind=CitationKind(payload.grounding_kind), ref=payload.grounding_ref
        )
        try:
            plan = tuple(
                PlannedStep(
                    description=str(step.get("description", "")),
                    categories=frozenset(
                        ActionCategory(str(c)) for c in step.get("categories", [])
                    ),
                )
                for step in item.get("plan", [])
                if isinstance(step, dict)
            )
            return InitiativeCandidate(
                observation=str(item.get("observation", "")),
                citations=(citation,),
                trigger=InitiativeTrigger(str(item.get("trigger", ""))),
                why_now=str(item.get("why_now", "")),
                plan=plan,
                next_step=str(item.get("next_step", "")),
                value=float(item.get("value", -1)),
                acceptance=float(item.get("acceptance", -1)),
                urgency=self._parse_urgency(item.get("urgency")),
                source=CandidateSource.EVENT,
                owner_id=owner,
                persona_id=payload.persona_id,
                prompt_version=EVENT_CANDIDATE_PROMPT_VERSION,
                scanned_at=datetime.now(UTC),
            )
        except (ValidationError, ValueError, TypeError, KeyError):
            # An unknown trigger/category, a missing field, an out-of-range estimate — every
            # malformation drops the candidate (conservative; the closed catalogue is by construct).
            _log.info("event candidate malformed; producing nothing (conservative)")
            return None

    @staticmethod
    def _parse_urgency(value: object) -> Urgency:
        """Unknown/missing urgency reads as BATCH — the conservative direction (A5 parity)."""
        try:
            return Urgency(str(value))
        except ValueError:
            return Urgency.BATCH

    @staticmethod
    def _load_json(text: str) -> dict[str, object] | None:
        cleaned = _FENCE_RE.sub("", text.strip()).strip()
        try:
            parsed = json.loads(cleaned)
        except (json.JSONDecodeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None


EVENT_CANDIDATE_SYSTEM_PROMPT = """\
You decide whether one platform EVENT that a persona is watching for warrants raising something \
UNPROMPTED to the user — an initiative. The event already matched a standing watch the user set \
up, so a reaction is expected; but restraint is still the product. A weak, presumptuous, or needy \
notice spends the user's trust at the worst exchange rate. Producing NOTHING is a correct and \
common outcome.

Return ONLY a JSON object of the form {"candidate": { ... }} or {"candidate": null} — no prose, \
no markdown fences. If nothing clears the bar: {"candidate": null}.

The candidate object has these fields:
- "observation": what was noticed, one plain sentence, STATED by the material below.
- "trigger": EXACTLY one of: "approaching_commitment", "task_followup", "conflict", \
"stale_open_loop". There are NO other triggers — an event that fits none is not a candidate.
- "why_now": what makes this timely (the event just happened / what it changed).
- "plan": the proposed steps, each {"description": ..., "categories": [...]} using ONLY: \
"observe", "compute", "draft", "notify_user" (safe) or "communicate_as_user", "spend", \
"external_mutate", "credentialed_access" (will require the user's confirmation).
- "next_step": the ONE concrete thing the user would see next. A pure FYI is not worth raising.
- "value": 0..1 — how much this matters to the user's LIFE (never to the conversation).
- "acceptance": 0..1 — honestly, would they welcome being told this NOW? Can only LOWER the odds.
- "urgency": "interrupt" ONLY for a hard dated commitment in the next ~2 days; else "batch".

RULES — follow every one exactly:
1. GROUNDED OR NOTHING. The observation must be STATED by the material. Never infer or read \
between the lines.
2. NEVER raise sensitive personal struggles (self-harm, a mental-health crisis, abuse, disordered \
eating, addiction) as the SUBJECT of an initiative. If the event material touches such topics, it \
is NOT what you bring up — return {"candidate": null}. This rule is absolute.
3. NEVER produce check-ins, conversation-starters, or anything whose purpose is interaction rather \
than the user's life and work — even if the user would welcome it.
4. AT MOST ONE candidate. If it is not clearly worth an unprompted notice, return null.

EXAMPLE (event → correct output):
- A message about a self-harm disclosure arrives → {"candidate": null}
- A routine newsletter arrives, nothing actionable → {"candidate": null}
"""
