"""The mechanical grounding check — no grounding, no candidate (Spec A5, T4; A5-D-2).

Two layers, the K2/A4-D-2 division of labour exactly: **mechanics admit, the
model decides — within the admitted set only, never conjuring grounds.**

1. **Resolution (pure-mechanical, no model).** Every citation must dereference
   through the injected :class:`GroundingSource` (the api implements it over
   the real graph/conversation/task read surfaces at T6/T7). A node that is
   merged or deleted (K7 moved on), a missing conversation, an unknown task —
   any single unresolvable citation discards the candidate BEFORE the judge is
   ever called.
2. **Entailment (the small-tier judge, conservative).** The judge sees the
   observation AND the text the user will read (the message, ``why_now``, and
   the offer, ``next_step``) + the mechanically-resolved excerpts, in ONE call
   (R9-249 re-review HIGH-B: since R9-256 the user reads the message, not the
   observation, so the message must be what is judged). It must answer with a
   VERBATIM supporting quote. The YES is accepted only when that quote
   mechanically appears in the admitted excerpts: a judge that says yes with
   a conjured quote is discarded the same as a no (the K2 evidence-span bar,
   enforced by substring check, not trust).
3. **The message's specifics, mechanically (before the judge).** Digits and
   English day and month names, as written, normalised first (thousands
   separators, "10:00" and "10am"), checked against the excerpts: see
   :mod:`persona_runtime.initiative.specifics`. The offer's numbers are checked
   too. A refusal needs no model call; the judge rules on everything else, with
   the user's local date in front of it so a relative claim can be verified.

Fail-soft is absolute (the kickoff rule: for initiative, silence IS the safe
state): a source error, a judge exception, malformed judge output — every
failure mode returns a discard verdict; nothing here ever raises to the scan
job, and no failure path can pass a candidate through.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from persona.logging import get_logger
from persona.schema.conversation import ConversationMessage
from pydantic import BaseModel, ConfigDict

from persona_runtime.initiative.specifics import unsupported_numbers, unsupported_specifics

if TYPE_CHECKING:
    from persona.backends.protocol import ChatBackend
    from persona.initiative import GroundingCitation, InitiativeCandidate

__all__ = [
    "ENTAILMENT_PROMPT_VERSION",
    "GroundingChecker",
    "GroundingRejection",
    "GroundingSource",
    "GroundingVerdict",
]

_logger = get_logger("runtime.initiative.grounding")

#: Bumped on any rule/prompt change; recorded with every verdict for traceability
#: (the Spec-10 versioned-artifact discipline; the A5-R-1 suite re-runs per version).
#: v3 (R9-249 third review LOW 2, LOW 8): the judge is told TODAY (the user's local date and
#: timezone) and refuses a relative claim it cannot verify against it; it sees the PLAN.
#: v2 (R9-249 re-review HIGH-B): one call judges the observation AND the message the user
#: reads (``why_now``) AND the offer (``next_step``), with a precise rule for hedged clauses.
ENTAILMENT_PROMPT_VERSION = "a5-entailment-v3"

_SYSTEM_PROMPT = """\
You verify whether cited excerpts GROUND an observation an assistant wants to raise, \
unprompted, with its user, and the words it will use. You are given: TODAY (the user's \
local date), the OBSERVATION (the assistant's grounds), the MESSAGE (the sentence the user \
will read), the OFFER (what the assistant offers to do) and the PLAN (its steps). The bar is \
strict. Inference, speculation, \
plausibility, or "reading between the lines" is NOT support. A wrong unprompted claim about \
someone's life costs trust; when in doubt, the answer is NO.

Answer entailed = true ONLY if ALL of these hold:
1. The excerpts STATE or DIRECTLY SUPPORT the OBSERVATION.
2. Every factual claim in the MESSAGE is stated or directly supported by the excerpts. Dates, \
times, amounts, names, deadlines, requirements and what someone said or did are all factual \
claims.
3. ONE exception to rule 2, a HEDGED CLAUSE: a clause that is introduced by "if", "unless" \
or "in case", is about the user's CURRENT state that the excerpts cannot settle (whether \
they have done something yet, whether something is still open or still wanted), and does \
not state that thing as true. Example: "if you haven't compared offers yet". A hedged \
clause may NOT introduce a date, time, amount, deadline or requirement the excerpts do not \
state: "if the form is due Friday" is a claim, not a hedge.
4. The OFFER and the PLAN only propose actions. They assert no fact the excerpts do not \
state.
5. A RELATIVE time claim in the MESSAGE ("today", "tomorrow", "this Friday", "next week", \
"in 3 days", "within the week") must be TRUE on TODAY, given the dates the excerpts state. \
If you cannot verify it from TODAY and the excerpts, rule 5 fails.

Reply with ONLY a JSON object, no prose:
{"entailed": true|false, "quote": "<a VERBATIM span copied exactly from the excerpts that \
states or directly supports the observation>"}
The quote must be copied character-for-character from the excerpts. If any rule fails, or \
the excerpts do not contain such a span, reply {"entailed": false}.
"""

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


class GroundingRejection(StrEnum):
    """Why a candidate was discarded (audited by the pipeline; never user-facing)."""

    UNRESOLVED_CITATION = "unresolved_citation"
    NOT_ENTAILED = "not_entailed"
    JUDGE_ERROR = "judge_error"
    #: The message states a number, weekday or month the excerpts never state (layer 3).
    UNSUPPORTED_SPECIFIC = "unsupported_specific"


class GroundingVerdict(BaseModel):
    """The check's outcome: admitted, or discarded with the mechanical reason."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    admitted: bool
    rejection: GroundingRejection | None = None
    #: The judge's verbatim supporting quote (admitted verdicts only) — the raw
    #: material of the user-facing honest attribution (K3's rule, rendered later).
    supporting_quote: str | None = None
    prompt_version: str = ENTAILMENT_PROMPT_VERSION


@runtime_checkable
class GroundingSource(Protocol):
    """The mechanical citation-resolution surface (api-implemented, T6/T7).

    Each method returns the citable stored CONTENT, or ``None`` when the
    reference does not resolve — and ``None`` is definitive: a merged node
    (K7 ``merged_into`` set), a deleted node, an unknown conversation/task all
    read as unresolvable, which discards the citing candidate.
    """

    def node_content(self, owner_id: str, node_id: str) -> str | None:
        """The node's current content; ``None`` for absent OR merged nodes."""
        ...

    def conversation_content(self, owner_id: str, conversation_id: str) -> str | None:
        """The conversation's stored content window; ``None`` when unknown."""
        ...

    def task_content(self, owner_id: str, task_id: str) -> str | None:
        """The task's goal + report/state summary; ``None`` when unknown."""
        ...


class GroundingChecker:
    """Resolution + entailment over an injected source and a small-tier backend."""

    def __init__(
        self,
        *,
        source: GroundingSource,
        backend: ChatBackend,
        max_output_tokens: int | None = None,
    ) -> None:
        """Inject the mechanical resolution surface and the judge backend.

        ``backend`` is the SMALL tier (the K2 synthesis precedent: the judge is
        one cheap deciding call over a mechanically-admitted set).
        ``max_output_tokens`` is the judge call's cap (R9-248: a reasoning model
        spends its reasoning inside it); ``None`` takes the
        ``InitiativeSettings.judge_max_output_tokens`` default, and the worker
        root passes the configured value.
        """
        from persona.initiative import InitiativeSettings

        self._source = source
        self._backend = backend
        self._max_output_tokens = (
            max_output_tokens
            if max_output_tokens is not None
            else InitiativeSettings.model_fields["judge_max_output_tokens"].default
        )

    async def check(
        self,
        candidate: InitiativeCandidate,
        *,
        today: date | None = None,
        timezone: str | None = None,
    ) -> GroundingVerdict:
        """No grounding, no candidate — never raises; every failure discards.

        Layer 1: resolve EVERY citation (any miss ⇒ discard, judge not called).
        Layer 3: the message's specifics and the offer's numbers must be supported
        by the excerpts (judge not called otherwise).
        Layer 2: the entailment judge over the observation, the message, the offer
        and the plan, told ``today`` (the user's local date; the pipeline passes it
        with ``timezone``; by default the candidate's own scan date in UTC); YES is
        accepted only with a verbatim-in-excerpts quote.
        """
        day = today if today is not None else candidate.scanned_at.date()
        zone = timezone or "UTC"
        try:
            excerpts = self._resolve_all(candidate)
        except Exception:  # noqa: BLE001 — a source failure discards, never crashes the scan
            _logger.warning("grounding source failed; discarding candidate (fail-soft)")
            return GroundingVerdict(
                admitted=False, rejection=GroundingRejection.UNRESOLVED_CITATION
            )
        if excerpts is None:
            return GroundingVerdict(
                admitted=False, rejection=GroundingRejection.UNRESOLVED_CITATION
            )
        unsupported = unsupported_specifics(
            candidate.why_now, excerpts, today=day
        ) + unsupported_numbers(candidate.next_step, excerpts)
        if unsupported:
            _logger.info(
                "message states specifics the excerpts do not; discarding ({n})",
                n=len(unsupported),
            )
            return GroundingVerdict(
                admitted=False, rejection=GroundingRejection.UNSUPPORTED_SPECIFIC
            )
        try:
            return await self._judge(candidate, excerpts, today=day, timezone=zone)
        except Exception:  # noqa: BLE001 — a judge failure discards, never crashes the scan
            _logger.warning("entailment judge failed; discarding candidate (fail-soft)")
            return GroundingVerdict(admitted=False, rejection=GroundingRejection.JUDGE_ERROR)

    # --- layer 1: mechanical resolution --------------------------------------

    def _resolve_all(self, candidate: InitiativeCandidate) -> list[str] | None:
        """Every citation dereferenced, in order; ``None`` if ANY fails to resolve."""
        excerpts: list[str] = []
        for citation in candidate.citations:
            content = self._resolve_one(candidate.owner_id, citation)
            if content is None:
                _logger.info(
                    "citation did not resolve; discarding candidate ref={ref}",
                    ref=citation.anchor,
                )
                return None
            excerpts.append(content)
        return excerpts

    def _resolve_one(self, owner_id: str, citation: GroundingCitation) -> str | None:
        from persona.initiative import CitationKind

        if citation.kind is CitationKind.NODE:
            return self._source.node_content(owner_id, citation.ref)
        if citation.kind is CitationKind.CONVERSATION:
            return self._source.conversation_content(owner_id, citation.ref)
        return self._source.task_content(owner_id, citation.ref)

    # --- layer 2: the entailment judge ---------------------------------------

    async def _judge(
        self, candidate: InitiativeCandidate, excerpts: list[str], *, today: date, timezone: str
    ) -> GroundingVerdict:
        """One deciding call over the admitted set; conservative parse; quote verified."""
        now = datetime.now(UTC)
        cited = "\n\n".join(f"[excerpt {i + 1}]\n{content}" for i, content in enumerate(excerpts))
        plan = "\n".join(f"- {step.description}" for step in candidate.plan)
        user = (
            f"TODAY (the user's local date, {timezone}): {today:%A} {today.day} "
            f"{today:%B %Y}\n\n"
            f"OBSERVATION (the grounds):\n{candidate.observation}\n\n"
            f"MESSAGE (what the user will read):\n{candidate.why_now}\n\n"
            f"OFFER (what the assistant offers to do):\n{candidate.next_step}\n\n"
            f"PLAN (the steps the assistant would take):\n{plan}\n\n"
            f"The cited excerpts (the ONLY admissible grounds):\n{cited}\n\n"
            "Reply with the JSON object."
        )
        messages = [
            ConversationMessage(role="system", content=_SYSTEM_PROMPT, created_at=now),
            ConversationMessage(role="user", content=user, created_at=now),
        ]
        response = await self._backend.chat(
            messages, temperature=0.0, max_tokens=self._max_output_tokens
        )
        payload = self._load_json(response.content)
        if payload is None:
            return GroundingVerdict(admitted=False, rejection=GroundingRejection.JUDGE_ERROR)
        if payload.get("entailed") is not True:
            return GroundingVerdict(admitted=False, rejection=GroundingRejection.NOT_ENTAILED)
        quote = str(payload.get("quote", "")).strip()
        # The conjured-grounds trap: a YES counts only when the quote mechanically
        # appears in the admitted excerpts (the K2 evidence-span bar, by substring
        # check — never by trusting the judge's own claim of verbatimness).
        if not quote or not any(quote in content for content in excerpts):
            _logger.info("judge YES with a non-verbatim quote; discarding (conjured grounds)")
            return GroundingVerdict(admitted=False, rejection=GroundingRejection.NOT_ENTAILED)
        return GroundingVerdict(admitted=True, supporting_quote=quote)

    @staticmethod
    def _load_json(text: str) -> dict[str, object] | None:
        cleaned = _FENCE_RE.sub("", text.strip()).strip()
        try:
            parsed = json.loads(cleaned)
        except (json.JSONDecodeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None
