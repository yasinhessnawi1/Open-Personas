"""The mechanical grounding check — no grounding, no candidate (Spec A5, T4; A5-D-2).

Two layers, the K2/A4-D-2 division of labour exactly: **mechanics admit, the
model decides — within the admitted set only, never conjuring grounds.**

1. **Resolution (pure-mechanical, no model).** Every citation must dereference
   through the injected :class:`GroundingSource` (the api implements it over
   the real graph/conversation/task read surfaces at T6/T7). A node that is
   merged or deleted (K7 moved on), a missing conversation, an unknown task —
   any single unresolvable citation discards the candidate BEFORE the judge is
   ever called.
2. **Entailment (the small-tier judge, conservative).** The judge sees ONLY the
   observation + the mechanically-resolved excerpts, and must answer with a
   VERBATIM supporting quote. The YES is accepted only when that quote
   mechanically appears in the admitted excerpts — a judge that says yes with
   a conjured quote is discarded the same as a no (the K2 evidence-span bar,
   enforced by substring check, not trust).

Fail-soft is absolute (the kickoff rule: for initiative, silence IS the safe
state): a source error, a judge exception, malformed judge output — every
failure mode returns a discard verdict; nothing here ever raises to the scan
job, and no failure path can pass a candidate through.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from persona.logging import get_logger
from persona.schema.conversation import ConversationMessage
from pydantic import BaseModel, ConfigDict

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
ENTAILMENT_PROMPT_VERSION = "a5-entailment-v1"

_SYSTEM_PROMPT = """\
You verify whether cited excerpts GROUND an observation an assistant wants to raise with \
its user. The bar is strict: the excerpts must STATE or DIRECTLY SUPPORT the observation. \
Inference, speculation, plausibility, or "reading between the lines" is NOT support. A wrong \
unprompted claim about someone's life costs trust; when in doubt, the answer is NO.

Reply with ONLY a JSON object, no prose:
{"entailed": true|false, "quote": "<a VERBATIM span copied exactly from the excerpts that \
states or directly supports the observation>"}
The quote must be copied character-for-character from the excerpts. If the excerpts do not \
contain such a span, reply {"entailed": false}.
"""

_MAX_TOKENS = 300
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


class GroundingRejection(StrEnum):
    """Why a candidate was discarded (audited by the pipeline; never user-facing)."""

    UNRESOLVED_CITATION = "unresolved_citation"
    NOT_ENTAILED = "not_entailed"
    JUDGE_ERROR = "judge_error"


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

    def __init__(self, *, source: GroundingSource, backend: ChatBackend) -> None:
        """Inject the mechanical resolution surface and the judge backend.

        ``backend`` is the SMALL tier (the K2 synthesis precedent — the judge is
        one cheap deciding call over a mechanically-admitted set).
        """
        self._source = source
        self._backend = backend

    async def check(self, candidate: InitiativeCandidate) -> GroundingVerdict:
        """No grounding, no candidate — never raises; every failure discards.

        Layer 1: resolve EVERY citation (any miss ⇒ discard, judge not called).
        Layer 2: the entailment judge over the admitted excerpts; YES is accepted
        only with a verbatim-in-excerpts quote.
        """
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
        try:
            return await self._judge(candidate.observation, excerpts)
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

    async def _judge(self, observation: str, excerpts: list[str]) -> GroundingVerdict:
        """One deciding call over the admitted set; conservative parse; quote verified."""
        now = datetime.now(UTC)
        cited = "\n\n".join(f"[excerpt {i + 1}]\n{content}" for i, content in enumerate(excerpts))
        user = (
            f"Observation the assistant wants to raise:\n{observation}\n\n"
            f"The cited excerpts (the ONLY admissible grounds):\n{cited}\n\n"
            "Reply with the JSON object."
        )
        messages = [
            ConversationMessage(role="system", content=_SYSTEM_PROMPT, created_at=now),
            ConversationMessage(role="user", content=user, created_at=now),
        ]
        response = await self._backend.chat(messages, temperature=0.0, max_tokens=_MAX_TOKENS)
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
