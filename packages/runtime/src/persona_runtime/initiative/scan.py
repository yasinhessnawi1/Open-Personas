"""The initiative scan: two inputs, one small-tier call, bounded output (Spec A5, T5).

The scan asks "what would a thoughtful assistant who knows this person
notice?" over two read surfaces (both injected Protocols; the api implements
them at T6): the graph noticing pool (the additive ``recent_nodes`` read —
salience+recency ordered, SELF/merged/wellbeing-tagged excluded AT the read —
expanded with its temporal/entity/causal lens links) and task history (active +
the additive ``list_recent_terminal``).

Conversation summaries are NOT an input (R9-249 review ruling, precision first).
A compacted summary carries no wellbeing tag of its own, and whether its
conversation disclosed anything is only as strong as K2's live tags (a deleted
flagged node, an extraction that returned nothing, a missed tag). A5-D-2 already
says a summary never grounds; now it is not shown either. Conversations reach the
scan only through the nodes synthesis learned from them.

Structural guarantees, each pinned by a T5 test:

- **Thin material ⇒ zero candidates without a model call** (criterion 1): no
  inputs means nothing to notice — the scan returns silence for free.
- **Citations come from the actual material only** (R9-248): the material names
  every item by a short PER-SCAN alias (``[N1]`` note, ``[T1]`` task), never by
  its real id. A citation resolves only by EXACT lookup of its normalised alias in
  this scan's map, keeping only aliases whose own line survived the input budget's
  truncation whole; the few-shot examples' codes are never issued. Normalisation
  strips surrounding ASCII spaces and brackets and one leading label word ("node
  N1") and nothing else: no prefix, no fuzzy match. Anything else (an unissued
  alias, a real or real-looking id, a truncated id) voids the candidate HERE,
  before T4's checker even runs (two mechanical layers against fabrication). The
  candidate carries the REAL id, so opportunity keys (decline memory, the
  duplicate slot) do not depend on alias numbering.
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
    EXAMPLE_ALIASES,
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
    "normalise_alias",
    "ScanGraphReader",
    "ScanLink",
    "ScanNode",
    "ScanTask",
    "ScanTaskReader",
]

_logger = get_logger("runtime.initiative.scan")

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
#: A complete alias: the kind letter then the per-kind ordinal (``N1``, ``T3``). ASCII
#: digits only: fullwidth or other-script digits never form an alias.
_ALIAS_RE = re.compile(r"[NT][0-9]+")
#: One leading label word a model may copy in front of the code ("node N1", "task: T1").
_ALIAS_LABEL_RE = re.compile(r"(?:NODE|TASK)(?=[\s_:#/\[-])")
_ALIAS_EDGE = " \t\r\n[]()"
_ALIAS_SEPARATORS = " \t_:#/-"
#: The kinds the material shows. Conversations are not shown (R9-249 review ruling),
#: so no conversation alias exists and a conversation citation never resolves.
_ALIAS_PREFIX: dict[CitationKind, str] = {
    CitationKind.NODE: "N",
    CitationKind.TASK: "T",
}
_CHARS_PER_TOKEN = 4  # the documented approximation bounding the prompt build
_NODE_SNIPPET = 300
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
class ScanTaskReader(Protocol):
    """Task history: active tasks + recent terminals (the A5-D-X-reads lens)."""

    def task_history(self, owner_id: str, *, limit: int) -> Sequence[ScanTask]:
        """Active tasks first, then the most recent terminal ones, bounded."""
        ...


def normalise_alias(raw: object) -> str | None:
    """The alias a citation names, or ``None`` if it names none (R9-248).

    Uppercase; strip surrounding ASCII whitespace and brackets; strip ONE leading
    label word (``node``, ``task``) and its separator; the rest must be exactly one
    complete alias. Nothing is inferred: no prefix matching, no fuzzy matching, no
    real ids, no leading zeros being read away, no non-ASCII digits or spaces.
    """
    token = str(raw).strip(_ALIAS_EDGE).upper()
    label = _ALIAS_LABEL_RE.match(token)
    if label is not None:
        token = token[label.end() :].lstrip(_ALIAS_SEPARATORS).strip(_ALIAS_EDGE)
    return token if _ALIAS_RE.fullmatch(token) else None


class _AliasMap:
    """One scan's alias to real-id map: the only legal citation targets (R9-248)."""

    def __init__(self) -> None:
        self._alias_of: dict[tuple[CitationKind, str], str] = {}
        self._real_of: dict[str, tuple[CitationKind, str]] = {}
        self._counts: dict[CitationKind, int] = dict.fromkeys(_ALIAS_PREFIX, 0)
        self._lines: dict[str, list[str]] = {}

    def alias(self, kind: CitationKind, real_id: str) -> str:
        """The item's alias, issued on first sight (one item, one alias).

        A code the prompt's few-shot examples use is never issued, so a model that
        parrots an example cites nothing real.
        """
        key = (kind, real_id)
        if key not in self._alias_of:
            code = self._next_code(kind)
            self._alias_of[key] = code
            self._real_of[code] = key
        return self._alias_of[key]

    def _next_code(self, kind: CitationKind) -> str:
        while True:
            self._counts[kind] += 1
            code = f"{_ALIAS_PREFIX[kind]}{self._counts[kind]}"
            if code not in EXAMPLE_ALIASES:
                return code

    def shows(self, code: str, line: str) -> str:
        """Record ``line`` as a line that shows ``code``'s item; returns the line."""
        self._lines.setdefault(code, []).append(line)
        return line

    def retain_shown(self, material: str) -> None:
        """Keep only aliases one of whose own lines survived the input budget whole.

        The material is cut at a character budget, so a line can be cut mid-text; an
        item is citable only if a line showing it (its own line, or a lens line naming
        it) is in the material complete. A bare ``[N7]`` surviving at the cut is not.
        """
        whole = set(material.split("\n"))
        self._real_of = {
            code: key
            for code, key in self._real_of.items()
            if any(line in whole for line in self._lines.get(code, ()))
        }

    def resolve(self, raw_ref: object) -> tuple[CitationKind, str] | None:
        """The ``(kind, real id)`` a citation names, by exact alias lookup only."""
        code = normalise_alias(raw_ref)
        return None if code is None else self._real_of.get(code)


def _one_line(text: str) -> str:
    """An item's text on ONE line, so "its own line survived" is a whole-line test."""
    return " ".join(text.splitlines())


class InitiativeScanner:
    """Two injected readers + the small-tier backend → zero-or-more candidates."""

    def __init__(
        self,
        *,
        graph: ScanGraphReader,
        tasks: ScanTaskReader,
        backend: ChatBackend,
        settings: InitiativeSettings,
    ) -> None:
        """Inject the two read surfaces, the SMALL-tier backend, and the knobs."""
        self._graph = graph
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
            pool, tasks = self._gather(owner_id)
        except Exception:  # noqa: BLE001 — a reader failure is silence, never a crash
            _logger.warning("scan input read failed; returning silence (fail-soft)")
            return ()
        if not pool and not tasks:
            # Thin material is SUCCESS: nothing to notice, no model call spent.
            return ()
        try:
            aliases = _AliasMap()
            response = await self._backend.chat(
                self._messages(pool, tasks, now=fire_time, aliases=aliases),
                temperature=0.0,
                max_tokens=self._settings.scan_max_output_tokens,
            )
            return self._parse(
                response.content,
                owner_id=owner_id,
                persona_id=persona_id,
                fire_time=fire_time,
                aliases=aliases,
            )
        except Exception:  # noqa: BLE001 — a model failure is silence, never a crash
            _logger.warning("scan model call failed; returning silence (fail-soft)")
            return ()

    # --- inputs ---------------------------------------------------------------

    def _gather(self, owner_id: str) -> tuple[list[ScanNode], list[ScanTask]]:
        limit = self._settings.recent_nodes_limit
        pool = [
            node
            for node in self._graph.noticing_pool(owner_id, limit=limit)
            if self._admissible(node)
        ]
        tasks = list(self._tasks.task_history(owner_id, limit=10))
        return pool, tasks

    @staticmethod
    def _admissible(node: ScanNode) -> bool:
        """Defense-in-depth (criterion 6, scan side): tagged/SELF never reach the prompt."""
        if node.wellbeing_category is not None or node.kind == "self":
            _logger.warning(
                "reader returned an excluded node; dropping id={node_id}", node_id=node.id
            )
            return False
        return True

    def _messages(
        self,
        pool: Sequence[ScanNode],
        tasks: Sequence[ScanTask],
        *,
        now: datetime,
        aliases: _AliasMap,
    ) -> list[ConversationMessage]:
        budget = self._settings.scan_max_input_tokens * _CHARS_PER_TOKEN
        sections: list[str] = [f"Today is {now.date().isoformat()}."]
        graph_lines: list[str] = []
        node = CitationKind.NODE
        for item in pool:
            code = aliases.alias(node, item.id)
            content = _one_line(item.content[:_NODE_SNIPPET])
            graph_lines.append(aliases.shows(code, f"[{code}] ({item.kind}) {content}"))
            for link in item.links:
                target = aliases.alias(node, link.target_id)
                text = _one_line(link.target_content[:_NODE_SNIPPET])
                graph_lines.append(
                    aliases.shows(target, f"  -> {link.link_type} [{target}] {text}")
                )
        if graph_lines:
            sections.append("GRAPH NOTES:\n" + "\n".join(graph_lines))
        if tasks:
            task_lines = []
            for t in tasks:
                code = aliases.alias(CitationKind.TASK, t.id)
                goal, summary = (
                    _one_line(t.goal[:_TASK_SNIPPET]),
                    _one_line(t.summary[:_TASK_SNIPPET]),
                )
                task_lines.append(aliases.shows(code, f"[{code}] ({t.status}) {goal} | {summary}"))
            sections.append("TASK HISTORY:\n" + "\n".join(task_lines))
        body = "\n\n".join(sections)
        if len(body) > budget:
            # Bounded by construction: truncate the material, never the rules.
            body = body[:budget]
        aliases.retain_shown(body)
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
        aliases: _AliasMap,
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
                aliases=aliases,
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
        aliases: _AliasMap,
    ) -> InitiativeCandidate | None:
        """One raw item → a validated candidate, or None (drop, conservative)."""
        if not isinstance(item, dict):
            return None
        try:
            # Fabrication voids the candidate HERE: every citation must name an
            # alias this scan issued and showed (T4 then re-vets the REAL id against
            # the real stores). The alias decides the kind; a declared kind is ignored.
            resolved: list[GroundingCitation] = []
            for raw in item.get("citations", []):
                if not isinstance(raw, dict):
                    continue
                target = aliases.resolve(raw.get("ref", ""))
                if target is None:
                    _logger.info(
                        "scan cited a code it was never given; dropping ref={ref}",
                        ref=str(raw.get("ref", ""))[:80],
                    )
                    return None
                resolved.append(GroundingCitation(kind=target[0], ref=target[1]))
            citations = tuple(resolved)
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
