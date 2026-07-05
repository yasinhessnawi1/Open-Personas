"""The initiative scan — three inputs, one small-tier call, bounded output (Spec A5, T5).

The scan asks "what would a thoughtful assistant who knows this person
notice?" over three read surfaces (all injected Protocols; the api implements
them at T6): the graph noticing pool (the additive ``recent_nodes`` read —
salience+recency ordered, SELF/merged/wellbeing-tagged excluded AT the read —
expanded with its temporal/entity/causal lens links), recent conversation
summaries (existing seams only — context, never grounds by itself), and task
history (active + the additive ``list_recent_terminal``).

Structural guarantees, each pinned by a T5 test:

- **Thin material ⇒ zero candidates without a model call** (criterion 1): no
  inputs means nothing to notice — the scan returns silence for free.
- **Citations come from the actual material only**: every cited ref must be an
  id the scan itself supplied; an invented ref voids the candidate HERE,
  before T4's checker even runs (two mechanical layers against fabrication).
- **The subject exclusion is defense-in-depth** (criterion 6, scan side): the
  read excludes wellbeing-tagged nodes; the scanner ALSO drops any tagged or
  SELF node a misbehaving reader returns — a tagged node never reaches the
  prompt.
- **Fail-soft absolute**: a reader error, a backend error, garbage output —
  every failure returns ``()``; the scan never raises and never fabricates.

Cost is bounded by construction: per-item truncation + a whole-prompt budget
(``scan_max_input_tokens``, applied as a ~4-chars/token approximation — the
metering row records real usage; A5-R-4 replaces estimates with data) and the
hard candidate cap (``scan_candidate_cap``, rank-ordered, the SOTA ≤3 rule).
"""

from __future__ import annotations

import json
import re
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
from pydantic import BaseModel, ConfigDict, ValidationError

from persona_runtime.initiative.scan_prompt import (
    INITIATIVE_SCAN_PROMPT_VERSION,
    SCAN_SYSTEM_PROMPT,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from persona.backends.protocol import ChatBackend
    from persona.initiative import InitiativeSettings

__all__ = [
    "InitiativeScanner",
    "ScanConversation",
    "ScanConversationReader",
    "ScanGraphReader",
    "ScanLink",
    "ScanNode",
    "ScanTask",
    "ScanTaskReader",
]

_logger = get_logger("runtime.initiative.scan")

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
_CHARS_PER_TOKEN = 4  # the documented approximation bounding the prompt build
_NODE_SNIPPET = 300
_SUMMARY_SNIPPET = 500
_TASK_SNIPPET = 200


class ScanLink(BaseModel):
    """One typed lens edge from a pool node (temporal/entity/causal, open-only)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    link_type: str
    target_id: str
    target_content: str


class ScanNode(BaseModel):
    """A noticing-pool node digest with its lens links (reader-supplied)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    kind: str
    content: str
    #: Carried ONLY for the defense-in-depth drop — the read already excludes
    #: tagged nodes; a non-None here means a misbehaving reader, and the
    #: scanner drops the node before the prompt (criterion 6, scan side).
    wellbeing_category: str | None = None
    links: tuple[ScanLink, ...] = ()


class ScanConversation(BaseModel):
    """A recent conversation's compacted summary (context; grounds only by id)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    summary: str


class ScanTask(BaseModel):
    """A task-history digest (active or recently terminal)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    goal: str
    status: str
    summary: str


@runtime_checkable
class ScanGraphReader(Protocol):
    """The graph input (api: ``recent_nodes`` + lens ``neighbors``, T6)."""

    def noticing_pool(self, owner_id: str, *, limit: int) -> Sequence[ScanNode]:
        """The salience+recency pool WITH lens links; subject-safe at the read."""
        ...


@runtime_checkable
class ScanConversationReader(Protocol):
    """Recent conversation summaries (existing seams only — Phase-1 ruling 3)."""

    def recent_summaries(self, owner_id: str, *, limit: int) -> Sequence[ScanConversation]:
        """Newest-first compacted summaries for the owner's recent conversations."""
        ...


@runtime_checkable
class ScanTaskReader(Protocol):
    """Task history: active tasks + recent terminals (the A5-D-X-reads lens)."""

    def task_history(self, owner_id: str, *, limit: int) -> Sequence[ScanTask]:
        """Active tasks first, then the most recent terminal ones, bounded."""
        ...


class InitiativeScanner:
    """Three injected readers + the small-tier backend → zero-or-more candidates."""

    def __init__(
        self,
        *,
        graph: ScanGraphReader,
        conversations: ScanConversationReader,
        tasks: ScanTaskReader,
        backend: ChatBackend,
        settings: InitiativeSettings,
    ) -> None:
        """Inject the three read surfaces, the SMALL-tier backend, and the knobs."""
        self._graph = graph
        self._conversations = conversations
        self._tasks = tasks
        self._backend = backend
        self._settings = settings

    async def scan(
        self, owner_id: str, persona_id: str, *, fire_time: datetime
    ) -> tuple[InitiativeCandidate, ...]:
        """Run one scan; never raises — every failure degrades to silence.

        ``fire_time`` is the schedule's fire instant (A1's handoff anchor); it
        stamps every produced candidate's ``scanned_at``.
        """
        try:
            pool, summaries, tasks = self._gather(owner_id)
        except Exception:  # noqa: BLE001 — a reader failure is silence, never a crash
            _logger.warning("scan input read failed; returning silence (fail-soft)")
            return ()
        if not pool and not summaries and not tasks:
            # Thin material is SUCCESS: nothing to notice, no model call spent.
            return ()
        try:
            response = await self._backend.chat(
                self._messages(pool, summaries, tasks, now=fire_time),
                temperature=0.0,
                max_tokens=1200,
            )
            return self._parse(
                response.content,
                owner_id=owner_id,
                persona_id=persona_id,
                fire_time=fire_time,
                known_refs=self._known_refs(pool, summaries, tasks),
            )
        except Exception:  # noqa: BLE001 — a model failure is silence, never a crash
            _logger.warning("scan model call failed; returning silence (fail-soft)")
            return ()

    # --- inputs ---------------------------------------------------------------

    def _gather(
        self, owner_id: str
    ) -> tuple[list[ScanNode], list[ScanConversation], list[ScanTask]]:
        limit = self._settings.recent_nodes_limit
        pool = [
            node
            for node in self._graph.noticing_pool(owner_id, limit=limit)
            if self._admissible(node)
        ]
        summaries = [
            s for s in self._conversations.recent_summaries(owner_id, limit=10) if s.summary
        ]
        tasks = list(self._tasks.task_history(owner_id, limit=10))
        return pool, summaries, tasks

    @staticmethod
    def _admissible(node: ScanNode) -> bool:
        """Defense-in-depth (criterion 6, scan side): tagged/SELF never reach the prompt."""
        if node.wellbeing_category is not None or node.kind == "self":
            _logger.warning(
                "reader returned an excluded node; dropping id={node_id}", node_id=node.id
            )
            return False
        return True

    @staticmethod
    def _known_refs(
        pool: Sequence[ScanNode],
        summaries: Sequence[ScanConversation],
        tasks: Sequence[ScanTask],
    ) -> dict[CitationKind, set[str]]:
        """Every id the scan actually supplied — the only legal citation targets."""
        node_ids = {n.id for n in pool} | {link.target_id for n in pool for link in n.links}
        return {
            CitationKind.NODE: node_ids,
            CitationKind.CONVERSATION: {s.id for s in summaries},
            CitationKind.TASK: {t.id for t in tasks},
        }

    def _messages(
        self,
        pool: Sequence[ScanNode],
        summaries: Sequence[ScanConversation],
        tasks: Sequence[ScanTask],
        *,
        now: datetime,
    ) -> list[ConversationMessage]:
        budget = self._settings.scan_max_input_tokens * _CHARS_PER_TOKEN
        sections: list[str] = [f"Today is {now.date().isoformat()}."]
        graph_lines: list[str] = []
        for node in pool:
            line = f"[node {node.id}] ({node.kind}) {node.content[:_NODE_SNIPPET]}"
            for link in node.links:
                line += (
                    f"\n  -> {link.link_type} [node {link.target_id}] "
                    f"{link.target_content[:_NODE_SNIPPET]}"
                )
            graph_lines.append(line)
        if graph_lines:
            sections.append("GRAPH NOTES:\n" + "\n".join(graph_lines))
        if summaries:
            sections.append(
                "RECENT CONVERSATIONS:\n"
                + "\n".join(
                    f"[conversation {s.id}] {s.summary[:_SUMMARY_SNIPPET]}" for s in summaries
                )
            )
        if tasks:
            sections.append(
                "TASK HISTORY:\n"
                + "\n".join(
                    f"[task {t.id}] ({t.status}) {t.goal[:_TASK_SNIPPET]} — "
                    f"{t.summary[:_TASK_SNIPPET]}"
                    for t in tasks
                )
            )
        body = "\n\n".join(sections)
        if len(body) > budget:
            # Bounded by construction: truncate the material, never the rules.
            body = body[:budget]
        user = f"{body}\n\nReply with the JSON object."
        return [
            ConversationMessage(role="system", content=SCAN_SYSTEM_PROMPT, created_at=now),
            ConversationMessage(role="user", content=user, created_at=now),
        ]

    # --- output ---------------------------------------------------------------

    def _parse(
        self,
        text: str,
        *,
        owner_id: str,
        persona_id: str,
        fire_time: datetime,
        known_refs: dict[CitationKind, set[str]],
    ) -> tuple[InitiativeCandidate, ...]:
        payload = self._load_json(text)
        if payload is None:
            _logger.warning("scan output unparseable; returning silence (fail-soft)")
            return ()
        raw = payload.get("candidates")
        if not isinstance(raw, list):
            return ()
        out: list[InitiativeCandidate] = []
        for item in raw[: self._settings.scan_candidate_cap]:
            candidate = self._build_candidate(
                item,
                owner_id=owner_id,
                persona_id=persona_id,
                fire_time=fire_time,
                known_refs=known_refs,
            )
            if candidate is not None:
                out.append(candidate)
        return tuple(out)

    def _build_candidate(
        self,
        item: object,
        *,
        owner_id: str,
        persona_id: str,
        fire_time: datetime,
        known_refs: dict[CitationKind, set[str]],
    ) -> InitiativeCandidate | None:
        """One raw item → a validated candidate, or None (drop, conservative)."""
        if not isinstance(item, dict):
            return None
        try:
            citations = tuple(
                GroundingCitation(
                    kind=CitationKind(str(c.get("kind", ""))), ref=str(c.get("ref", ""))
                )
                for c in item.get("citations", [])
                if isinstance(c, dict)
            )
            # Fabrication voids the candidate HERE: every ref must be an id the
            # scan itself supplied (T4 then re-vets against the real stores).
            for citation in citations:
                if citation.ref not in known_refs[citation.kind]:
                    _logger.info(
                        "scan cited an id it was never given; dropping ref={ref}",
                        ref=citation.anchor,
                    )
                    return None
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
                citations=citations,
                trigger=InitiativeTrigger(str(item.get("trigger", ""))),
                why_now=str(item.get("why_now", "")),
                plan=plan,
                next_step=str(item.get("next_step", "")),
                value=float(item.get("value", -1)),
                acceptance=float(item.get("acceptance", -1)),
                urgency=self._parse_urgency(item.get("urgency")),
                source=CandidateSource.SCAN,
                owner_id=owner_id,
                persona_id=persona_id,
                prompt_version=INITIATIVE_SCAN_PROMPT_VERSION,
                scanned_at=fire_time,
            )
        except (ValidationError, ValueError, TypeError, KeyError):
            # An unknown trigger/category, a missing field, an out-of-range
            # estimate — every malformation drops the candidate (conservative;
            # the closed catalogue is enforced by construction).
            _logger.info("scan candidate malformed; dropping (conservative)")
            return None

    @staticmethod
    def _parse_urgency(value: object) -> Urgency:
        """Unknown/missing urgency reads as BATCH — the conservative direction."""
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
