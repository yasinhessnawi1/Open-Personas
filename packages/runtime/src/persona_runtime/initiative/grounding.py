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

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from persona.logging import get_logger
from persona.schema.conversation import ConversationMessage
from pydantic import BaseModel, ConfigDict

from persona_runtime.initiative.specifics import (
    relative_day_words,
    states_a_date,
    time_words,
    unsupported_numbers,
    unsupported_specifics,
    vague_hedges,
)
from persona_runtime.routing.model_selection import resolve_served_model

if TYPE_CHECKING:
    from collections.abc import Sequence

    from persona.backends.protocol import ChatBackend
    from persona.backends.types import ChatResponse
    from persona.initiative import GroundingCitation, InitiativeCandidate

__all__ = [
    "ContextNote",
    "ENTAILMENT_PROMPT_VERSION",
    "MAX_CONTEXT_NOTES",
    "MAX_CONTEXT_PER_NODE",
    "excerpts_fingerprint",
    "MIN_QUOTE_PART_CHARS",
    "quote_in_excerpts",
    "GroundingChecker",
    "GroundingRejection",
    "GroundingSource",
    "GroundingVerdict",
]

_logger = get_logger("runtime.initiative.grounding")

#: Bumped on any rule/prompt change; recorded with every verdict for traceability
#: (the Spec-10 versioned-artifact discipline; the A5-R-1 suite re-runs per version).
#: v4 (owner ruling 2026-10-30, F3): an advisory clause after a stated deadline, a weekday
#: matching a stated date, and a comparison across excerpts are support; (F2) the judge also
#: sees CONTEXT, the newest notes linked to
#: each cited node (never grounds, never quoted), and answers no when it shows the grounds no
#: longer hold (A5-R-1 re-run B7: a slides note for a job the user had left).
#: v3 (R9-249 third review LOW 2, LOW 8): the judge is told TODAY (the user's local date and
#: timezone) and refuses a relative claim it cannot verify against it; it sees the PLAN.
#: v2 (R9-249 re-review HIGH-B): one call judges the observation AND the message the user
#: reads (``why_now``) AND the offer (``next_step``), with a precise rule for hedged clauses.
ENTAILMENT_PROMPT_VERSION = "a5-entailment-v4"

_SYSTEM_PROMPT = """\
You verify whether cited excerpts GROUND an observation an assistant wants to raise, \
unprompted, with its user, and the words it will use. You are given: TODAY (the user's \
local date), the OBSERVATION (the assistant's grounds), the MESSAGE (the sentence the user \
will read), the OFFER (what the assistant offers to do), the PLAN (its steps), the cited \
excerpts and, when there are any, CONTEXT notes linked to them. The bar is \
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
4. A second exception to rule 2, an ADVISORY CLAUSE after a stated deadline or fact: one \
short clause such as "so you may want to start soon", "there is still time to compare \
offers", "you still have a few days" or "paying it now avoids the reminder fee". It holds \
when the deadline or consequence it relies on is stated in the excerpts and the clause is \
TRUE on TODAY. It may NOT add a deadline, amount, consequence or requirement the excerpts do \
not state, and a date the excerpts only guess at ("might be before Christmas") stays a claim.
5. Arithmetic on stated facts is support, not inference: a weekday that matches a stated date \
(the date fixes the weekday), and a comparison of stated dates or times across excerpts (they \
overlap, one comes before the other, the gap between them). For a comparison, quote one line \
from each excerpt it uses, each on its own line.
6. The OFFER and the PLAN only propose actions. They assert no fact the excerpts do not \
state.
7. A RELATIVE time claim in the MESSAGE ("today", "tomorrow", "this Friday", "next week", \
"in 3 days", "within the week", "a few days left") must be TRUE on TODAY, given the dates the \
excerpts state. If you cannot verify it from TODAY and the excerpts, rule 7 fails.
8. The CONTEXT is NOT grounds and is never quoted. If it shows the grounds no longer hold \
(the person has left that job or role, moved, cancelled it, already done or finished it, or \
a newer note contradicts an excerpt), rule 8 fails.

Reply with ONLY a JSON object, no prose:
{"entailed": true|false, "quote": "<a VERBATIM span copied exactly from the excerpts that \
states or directly supports the observation>"}
The quote must be copied character-for-character from the excerpts (for a comparison, one \
line from each excerpt, on separate lines). If any rule fails, or the excerpts do not contain \
such a span, reply {"entailed": false}.
"""

#: The shortest quote, and the shortest line of a multi-line quote, normalised: every
#: line must stand as evidence on its own, so a fragment ("the", "2026") never carries a
#: YES (fourth review LOW 1: characters, not words; the shortest quote in the re-run
#: records is 40 characters).
MIN_QUOTE_PART_CHARS = 20
_QUOTE_MARKS = str.maketrans(
    {"\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
     "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"'}
)  # fmt: skip
_WHITESPACE_RE = re.compile(r"\s+")

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


class GroundingRejection(StrEnum):
    """Why a candidate was discarded (audited by the pipeline; never user-facing)."""

    UNRESOLVED_CITATION = "unresolved_citation"
    NOT_ENTAILED = "not_entailed"
    JUDGE_ERROR = "judge_error"
    #: The message states a number, weekday or month the excerpts never state (layer 3).
    UNSUPPORTED_SPECIFIC = "unsupported_specific"
    #: The message or the offer names a day relative to when it was written ("tomorrow",
    #: "this Friday", a weekday without its date): the user may read it a day later.
    RELATIVE_DATE = "relative_date"
    #: The judge's CONTEXT could not be read (a transient source error): the scan
    #: discards, the flush keeps the notice held for its next pass (review LOW-5).
    CONTEXT_UNAVAILABLE = "context_unavailable"


class GroundingVerdict(BaseModel):
    """The check's outcome: admitted, or discarded with the mechanical reason."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    admitted: bool
    rejection: GroundingRejection | None = None
    #: The judge's verbatim supporting quote (admitted verdicts only) — the raw
    #: material of the user-facing honest attribution (K3's rule, rendered later).
    supporting_quote: str | None = None
    prompt_version: str = ENTAILMENT_PROMPT_VERSION
    #: The fingerprint of the excerpts this verdict was reached on, and of the judge's
    #: prompt version (``None`` when no excerpt resolved). The pipeline stores it on the
    #: held notice, so the flush can tell whether anything the judge read has changed.
    fingerprint: str | None = None
    #: The chain member that served the judge call, as ``"provider/model"``, read from
    #: the chain's attempt ledger (:func:`served_identity`); ``None`` when no model
    #: answered (a mechanical layer decided, an earlier admission stood, the call
    #: failed) or the served member is unclear. The pipeline stores it with the
    #: fingerprint: a YES from any member but the head of the chain is never kept.
    served_model: str | None = None
    #: An earlier admission was kept without a model call (the flush audits it).
    kept: bool = False


#: The judge's CONTEXT bound (F2): the newest linked notes read per cited node, and shown
#: in all. Small on purpose: context can only make the judge say no.
MAX_CONTEXT_PER_NODE = 2
MAX_CONTEXT_NOTES = 4


@dataclass(frozen=True)
class ContextNote:
    """A note linked to a cited node, shown to the judge as CONTEXT, never as grounds."""

    written_on: date | None
    content: str


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

    def node_context(self, owner_id: str, node_id: str, *, limit: int) -> Sequence[ContextNote]:
        """Up to ``limit`` notes linked to the node one hop away (the scan's lenses),
        newest first; never a wellbeing-tagged note or one learned from a gated
        interaction. Empty when there are none."""
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
        admitted_fingerprint: str | None = None,
        admitted_served_model: str | None = None,
    ) -> GroundingVerdict:
        """No grounding, no candidate — never raises; every failure discards.

        Layer 1: resolve EVERY citation (any miss ⇒ discard, judge not called).
        Layer 3: the message's specifics and the offer's numbers must be supported
        by the excerpts (judge not called otherwise).
        Layer 2: the entailment judge over the observation, the message, the offer
        and the plan, told ``today`` (the user's local date; the pipeline passes it
        with ``timezone``; by default the candidate's own scan date in UTC); YES is
        accepted only with a verbatim-in-excerpts quote.

        ``admitted_fingerprint`` and ``admitted_served_model`` are the fingerprint an
        earlier YES was reached on and the model that gave it (the flush passes the
        held notice's). The earlier admission stands without a model call only when
        all of these hold (:meth:`_keeps_admission`; fourth review MEDIUM-1): the
        freshly resolved excerpts and the prompt version still match the fingerprint;
        the YES came from the head of the judge's chain, not a fallback; and neither
        the message nor the offer places itself in time (:func:`time_words`), so a
        dated message is always re-read against the new TODAY. Otherwise the judge
        is asked again.
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
        try:
            context = self._context(candidate, excerpts)
        except Exception:  # noqa: BLE001 - transient: never read as a dissolved citation
            _logger.warning("judge context unavailable; not judged this time (fail-soft)")
            return GroundingVerdict(
                admitted=False, rejection=GroundingRejection.CONTEXT_UNAVAILABLE
            )
        fingerprint = excerpts_fingerprint(excerpts, context)
        dated = states_a_date(candidate.why_now, candidate.next_step)
        relative = relative_day_words(candidate.why_now, dated=dated) + relative_day_words(
            candidate.next_step, dated=dated
        )
        hedges = vague_hedges(candidate.why_now)  # the offer's courtesy does not count
        if dated and len(hedges) > 1:  # at most one advisory clause after the date
            relative += hedges
        if relative:
            _logger.info("message names a relative day; discarding ({n})", n=len(relative))
            return GroundingVerdict(
                admitted=False, rejection=GroundingRejection.RELATIVE_DATE, fingerprint=fingerprint
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
                admitted=False,
                rejection=GroundingRejection.UNSUPPORTED_SPECIFIC,
                fingerprint=fingerprint,
            )
        try:
            keep = self._keeps_admission(
                candidate, fingerprint, admitted_fingerprint, admitted_served_model
            )
        except Exception:  # noqa: BLE001: undecidable (e.g. the plan read failed): judge again
            _logger.warning("could not decide whether to keep the verdict; judging again")
            keep = False
        if keep:
            _logger.info("grounding kept the admitted verdict (unchanged, head model, undated)")
            return GroundingVerdict(admitted=True, fingerprint=fingerprint, kept=True)
        try:
            verdict = await self._judge(candidate, excerpts, context, today=day, timezone=zone)
            return verdict.model_copy(update={"fingerprint": fingerprint})
        except Exception:  # noqa: BLE001 — a judge failure discards, never crashes the scan
            _logger.warning("entailment judge failed; discarding candidate (fail-soft)")
            return GroundingVerdict(admitted=False, rejection=GroundingRejection.JUDGE_ERROR)

    def _keeps_admission(
        self,
        candidate: InitiativeCandidate,
        fingerprint: str,
        admitted_fingerprint: str | None,
        admitted_served_model: str | None,
    ) -> bool:
        """Whether an earlier YES stands without asking the judge again (see :meth:`check`)."""
        if admitted_fingerprint is None or admitted_fingerprint != fingerprint:
            return False
        if admitted_served_model is None or admitted_served_model != head_identity(self._backend):
            return False  # a fallback member's YES (or an unrecorded one) is not final
        return not (time_words(candidate.why_now) or time_words(candidate.next_step))

    def _served(self, response: ChatResponse) -> str | None:
        """The served identity, ``None`` (re-judged at the flush) if it cannot be read."""
        try:
            return served_identity(resolve_chain(self._backend), response)
        except Exception:  # noqa: BLE001: an unreadable identity never discards a verdict
            _logger.warning("judge's served model could not be read; its YES will not be kept")
            return None

    # --- layer 1: mechanical resolution --------------------------------------

    def _context(self, candidate: InitiativeCandidate, excerpts: list[str]) -> list[ContextNote]:
        """The CONTEXT for the judge: each cited node's newest linked notes, bounded,
        without repeating an excerpt, newest first overall (F2)."""
        from persona.initiative import CitationKind

        notes: list[ContextNote] = []
        seen = set(excerpts)
        for citation in candidate.citations:
            if citation.kind is not CitationKind.NODE:
                continue
            for note in self._source.node_context(
                candidate.owner_id, citation.ref, limit=MAX_CONTEXT_PER_NODE
            ):
                if note.content not in seen:
                    seen.add(note.content)
                    notes.append(note)
        notes.sort(key=lambda n: n.written_on or date.min, reverse=True)
        return notes[:MAX_CONTEXT_NOTES]

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
        self,
        candidate: InitiativeCandidate,
        excerpts: list[str],
        context: list[ContextNote],
        *,
        today: date,
        timezone: str,
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
            f"{_render_context(context)}"
            "Reply with the JSON object."
        )
        messages = [
            ConversationMessage(role="system", content=_SYSTEM_PROMPT, created_at=now),
            ConversationMessage(role="user", content=user, created_at=now),
        ]
        response = await self._backend.chat(
            messages, temperature=0.0, max_tokens=self._max_output_tokens
        )
        served = self._served(response)
        payload = self._load_json(response.content)
        if payload is None:
            return GroundingVerdict(
                admitted=False, rejection=GroundingRejection.JUDGE_ERROR, served_model=served
            )
        if payload.get("entailed") is not True:
            return GroundingVerdict(
                admitted=False, rejection=GroundingRejection.NOT_ENTAILED, served_model=served
            )
        quote = str(payload.get("quote", "")).strip()
        # The conjured-grounds trap: a YES counts only when the quote mechanically
        # appears in the admitted excerpts (the K2 evidence-span bar, by substring
        # check, never by trusting the judge's own claim of verbatimness), compared
        # after the normalisation :func:`quote_in_excerpts` documents.
        if not quote_in_excerpts(quote, excerpts):
            _logger.info("judge YES with a non-verbatim quote; discarding (conjured grounds)")
            return GroundingVerdict(
                admitted=False, rejection=GroundingRejection.NOT_ENTAILED, served_model=served
            )
        return GroundingVerdict(admitted=True, supporting_quote=quote, served_model=served)

    @staticmethod
    def _load_json(text: str) -> dict[str, object] | None:
        cleaned = _FENCE_RE.sub("", text.strip()).strip()
        try:
            parsed = json.loads(cleaned)
        except (json.JSONDecodeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None


def _normalise_quote_text(text: str) -> str:
    """Casefold; curly quotes and apostrophes become straight; every whitespace run
    becomes one space (the whitespace pattern matches Unicode whitespace, so line breaks
    and no-break, narrow no-break and thin spaces collapse with the rest)."""
    text = text.translate(_QUOTE_MARKS).casefold()
    return _WHITESPACE_RE.sub(" ", text).strip()


def _unwrapped(quote: str) -> str | None:
    """The quote without one surrounding pair of quotation marks, if it has one."""
    stripped = quote.strip()
    marks = stripped.translate(_QUOTE_MARKS)
    if len(stripped) >= 2 and marks[0] == marks[-1] and marks[0] in "\"'":
        return stripped[1:-1]
    return None


def _quote_matches(quote: str, texts: list[str]) -> bool:
    parts = [_normalise_quote_text(part) for part in quote.splitlines() if part.strip()]
    if not parts or any(len(part) < MIN_QUOTE_PART_CHARS for part in parts):
        return False
    return all(any(part in text for text in texts) for part in parts)


def quote_in_excerpts(quote: str, excerpts: list[str]) -> bool:
    """Whether the judge's quote is verbatim in the admitted excerpts.

    Both sides are normalised (:func:`_normalise_quote_text`), so a judge that
    sentence-cases a clause or re-types a no-break space is not refused for it, and
    one surrounding pair of quotation marks (straight or curly) the judge wrapped it in
    is set aside. A quote spanning several excerpts is accepted when it splits on line
    breaks into parts that are each verbatim in some excerpt. Every part, a single-line
    quote's too, is at least ``MIN_QUOTE_PART_CHARS`` characters. Nothing else is
    inferred: a word changed, added or dropped still refuses the YES.
    """
    texts = [_normalise_quote_text(content) for content in excerpts]
    if _quote_matches(quote, texts):
        return True
    inner = _unwrapped(quote)
    return inner is not None and _quote_matches(inner, texts)


def resolve_chain(backend: ChatBackend) -> ChatBackend:
    """The concrete backend behind ``backend``, resolved once (sixth review LOW-4).

    The production judge runs behind a metering wrapper over a per-owner plan wrapper;
    both offer ``resolve_chain``, which reads the owner's plan once and returns the
    tier's chain. A backend without it is its own chain.
    """
    resolver = getattr(backend, "resolve_chain", None)
    return resolver() if callable(resolver) else backend


def head_identity(backend: ChatBackend) -> str:
    """The head of the judge's chain as ``"provider/model"`` (a chain reports its
    primary; a bare backend is its own head), from one resolution."""
    chain = resolve_chain(backend)
    return f"{chain.provider_name}/{chain.model_name}"


def served_identity(chain: ChatBackend, response: ChatResponse) -> str | None:
    """The chain member that served ``response``, as ``"provider/model"`` (fifth review
    LOW-1).

    Read from the chain's attempt ledger (:func:`resolve_served_model`: the primary
    served when the walk recorded no failed attempt), so a fallback to the same model on
    another provider is not the head. The response's own echo can only demote: when it
    names another member (the ledger is shared by concurrent calls on one chain, a
    registered gap), the member is unclear and ``None`` is returned, so the YES is
    judged again at the flush rather than kept on a guess.
    """
    provider, model = resolve_served_model(chain)
    if (response.provider, response.model) != (provider, model):
        _logger.warning("judge's served model is unclear; its YES will not be kept")
        return None
    return f"{provider}/{model}"


def _render_context(context: list[ContextNote]) -> str:
    if not context:
        return ""
    lines = "\n".join(
        f"[context {i + 1}] (noted {n.written_on.isoformat() if n.written_on else 'undated'}) "
        f"{n.content}"
        for i, n in enumerate(context)
    )
    return (
        "CONTEXT (newer notes linked to the cited ones; NOT grounds, never quote them):\n"
        f"{lines}\n\n"
    )


def excerpts_fingerprint(excerpts: list[str], context: Sequence[ContextNote] = ()) -> str:
    """A stable fingerprint of the admitted excerpts, in citation order, of the CONTEXT
    the judge was shown (F2: a newly linked note re-asks the judge), and of the judge's
    prompt version (sha256): a verdict reached under another prompt is not kept
    (fourth review LOW 5)."""
    digest = hashlib.sha256()
    digest.update(ENTAILMENT_PROMPT_VERSION.encode("utf-8"))
    digest.update(b"\x1d")
    for content in excerpts:
        digest.update(content.encode("utf-8"))
        digest.update(b"\x1e")
    digest.update(b"\x1c")
    for note in context:
        written = note.written_on.isoformat() if note.written_on else ""
        digest.update(f"{written}\x1f{note.content}".encode())
        digest.update(b"\x1e")
    return digest.hexdigest()
