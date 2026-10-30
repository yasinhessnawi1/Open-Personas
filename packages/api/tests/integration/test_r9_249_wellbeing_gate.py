"""R9-249 on real Postgres: what a gated conversation taught never becomes a notice.

A5-R-1 (paid run 1, B3) delivered a disordered-eating disclosure as an unprompted
initiative. Summaries are no longer scan material (the review ruling); conversations reach
the scan only through nodes, so the gate follows every item's SOURCE. One owner holds:

- ``conv-disclosed``: synthesised; K2 tagged the disclosure AND extracted an untagged
  sibling ("Mia's dinner party on Thursday") from it: the review's production-shape B3;
- ``conv-fresh`` (no marker), ``conv-behind`` (marker short), ``conv-regen`` (a reply
  regenerated after synthesis ran: the superseded row still counts, as synthesis counts
  it): all unsynthesised; ``conv-clean`` (a tool row that does not count) and
  ``conv-empty`` (no transcript at all) are not;
- nodes learned from those conversations, a flagged node learned from a task's run, and
  tasks begun in each conversation.

Proven: the freshness SQL, pinned to synthesis's own read and windowing; the scan's graph
reader (pool and lens targets) and task reader drop what a gated interaction taught; the
subject rule judges nodes, conversations and tasks by source (scan and event modes); and
both B3 shapes through the REAL pipeline are discarded with their audit row.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from persona.audit import MemoryAuditLogger
from persona.backends.types import ChatResponse, TokenUsage
from persona.extraction import InteractionKind
from persona.graph import build_graph_store
from persona.graph.models import (
    ConceptNode,
    LinkType,
    NodeKind,
    NodeProvenance,
    TypedLink,
    make_edge_id,
)
from persona.graph.postgres import PostgresGraphBackend
from persona.initiative import (
    CandidateSource,
    CitationKind,
    GroundingCitation,
    InitiativeCandidate,
    InitiativeDial,
    InitiativeSettings,
    InitiativeTrigger,
    PlannedStep,
    Urgency,
)
from persona.jobs.synthesis import make_conversation_synthesis_payload
from persona.schema.chunks import WriteSource
from persona.tasks import Contract, Task, TaskState
from persona.tools.categories import ActionCategory
from persona_api.db.engine import rls_connection
from persona_api.initiative.pipeline_wiring import (
    ApiGroundingSource,
    ApiPipelineAuditor,
    ApiProvenanceReader,
    ApiUserContextReader,
    ApiWellbeingSubjectCheck,
    LedgerAdapter,
)
from persona_api.initiative.readers import ApiScanGraphReader, ApiScanTaskReader
from persona_api.initiative.store import DeclineStore, InitiativeLedger
from persona_api.initiative.wellbeing_gate import ConversationWellbeingGate
from persona_api.jobs.handlers.synthesis import PgSynthesisRepository
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from persona_api.tasks.store import CheckpointStore, TaskStore
from persona_runtime.extraction.windowing import build_window
from persona_runtime.initiative import GroundingChecker, InitiativePipeline
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 9, 26, 5, 0, tzinfo=UTC)
_OWNER = "own_r9249"
_PERSONA = "pers_r9249"
_FLAGGED = f"{_OWNER}::node::00000001"
_PLAIN = f"{_OWNER}::node::00000002"
#: The production shape of B3: K2 tagged the disclosure, and beside it extracted this
#: UNTAGGED event node from the same conversation (review HIGH-1).
_DINNER = f"{_OWNER}::node::00000003"
_FRESH_NODE = f"{_OWNER}::node::00000004"
_LOOSE = f"{_OWNER}::node::00000005"
_RUN_FLAGGED = f"{_OWNER}::node::00000006"
_ALL = ("conv-disclosed", "conv-fresh", "conv-behind", "conv-clean", "conv-empty", "conv-regen")
_DIM = 384


class _Emb:
    model_name = "fake"

    @property
    def dimension(self) -> int:
        return _DIM

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[1.0] + [0.0] * (_DIM - 1) for _ in texts]


class _YesJudge:
    """Scripted judge: YES with a verbatim quote (admission stays mechanical)."""

    provider_name = "scripted"
    model_name = "scripted"

    async def chat(self, messages: Any, **_: object) -> ChatResponse:  # noqa: ANN401
        prompt = str(messages[-1].content)
        start = prompt.index("[excerpt 1]\n") + len("[excerpt 1]\n")
        block = prompt[start:].split("\n\n[excerpt ", 1)[0].split("\n\nReply with", 1)[0]
        quote = max(block.splitlines(), key=len)  # one whole line of the excerpt
        return ChatResponse(
            content=json.dumps({"entailed": True, "quote": quote}),
            model="scripted",
            provider="scripted",
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            latency_ms=1.0,
        )


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Iterator[Engine]:  # noqa: ARG001 (ordering)
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL (non-superuser role) not set; skipping RLS test")
    engine = make_rls_engine(app_url.replace("+asyncpg", "+psycopg"))
    yield engine
    engine.dispose()


def _node(
    node_id: str,
    content: str,
    *,
    wellbeing: str | None = None,
    source: str | None,
    kind: str = "conversation",
) -> ConceptNode:
    return ConceptNode(
        id=node_id,
        node_kind=NodeKind.CIRCUMSTANCE if wellbeing else NodeKind.FACT,
        concept_name=node_id.rsplit("::", 1)[-1],
        content=content,
        wellbeing_category=wellbeing,
        provenance=(
            NodeProvenance(
                source=WriteSource.PERSONA_SELF,
                persona_id=_PERSONA,
                interaction_id=source,
                interaction_kind=kind if source else None,
                written_at=_NOW,
            ),
        ),
        created_at=_NOW,
    )


#: (role, content, superseded) per conversation. conv-empty holds no transcript row;
#: conv-regen had its reply regenerated after synthesis ran (the old row is superseded).
_TURNS: dict[str, list[tuple[str, str, bool]]] = {
    "conv-disclosed": [("user", "Food has been hard again, the restricting is back.", False),
                       ("assistant", "Thank you for telling me.", False)],
    "conv-fresh": [("user", "My friend's dinner party is on Thursday.", False),
                   ("assistant", "Noted.", False)],
    "conv-behind": [("user", "One.", False), ("assistant", "Two.", False),
                    ("user", "Three.", False)],
    "conv-clean": [("user", "The EU-kontroll is due next week.", False),
                   ("assistant", "Worth booking early.", False),
                   ("tool", "lookup result", False)],
    "conv-empty": [("system", "Conversation opened.", False)],
    "conv-regen": [("user", "Plan my week.", False), ("assistant", "First draft.", True),
                   ("assistant", "Second draft.", False)],
}  # fmt: skip
_MARKERS = {"conv-disclosed": 2, "conv-behind": 2, "conv-clean": 2, "conv-regen": 2}


def _seed_conversations(conn: Any) -> None:  # noqa: ANN401
    for conv, rows in _TURNS.items():
        conn.execute(
            text(
                "INSERT INTO conversations (id, owner_id, persona_id, compacted_summary) "
                "VALUES (:c, :u, :p, :s)"
            ),
            {"c": conv, "u": _OWNER, "p": _PERSONA, "s": f"Summary of {conv}: {rows[0][1]}"},
        )
        for offset, (role, content, superseded) in enumerate(rows):
            conn.execute(
                text(
                    "INSERT INTO messages (conversation_id, role, content, created_at, "
                    "superseded_at) VALUES (:c, :r, :m, :t, :s)"
                ),
                {
                    "c": conv,
                    "r": role,
                    "m": content,
                    "t": _NOW + timedelta(seconds=offset),
                    "s": _NOW if superseded else None,
                },
            )
    for conv, count in _MARKERS.items():
        conn.execute(
            text(
                "INSERT INTO synthesis_markers (owner_id, interaction_kind, interaction_id, "
                "synthesised_up_to) VALUES (:u, 'conversation', :c, :n)"
            ),
            {"u": _OWNER, "c": conv, "n": count},
        )


def _seed_graph(super_engine: Engine) -> None:
    backend = PostgresGraphBackend(engine=super_engine)
    vec = [1.0] + [0.0] * (_DIM - 1)
    nodes = [
        _node(
            _FLAGGED,
            "The user disclosed restrictive eating.",
            wellbeing="disordered_eating",
            source="conv-disclosed",
        ),
        _node(
            _DINNER,
            "The user is attending Mia's dinner party on Thursday.",
            source="conv-disclosed",
        ),
        _node(_PLAIN, "The user works as a teacher.", source="conv-clean"),
        _node(_FRESH_NODE, "The user's friend hosts on Thursday.", source="conv-fresh"),
        _node(_LOOSE, "The user likes rainy mornings.", source=None),
        _node(
            _RUN_FLAGGED,
            "A research run surfaced the user's panic attacks.",
            wellbeing="mental_health",
            source="run-7",
            kind="agentic_run",
        ),
    ]
    for node in nodes:
        backend.insert_node_if_absent(_OWNER, node, vec)
    for dst in (_DINNER, _LOOSE):
        backend.upsert_edge(
            _OWNER,
            TypedLink(
                id=make_edge_id(_PLAIN, dst, LinkType.TEMPORAL),
                src_node_id=_PLAIN,
                dst_node_id=dst,
                link_type=LinkType.TEMPORAL,
                provenance=NodeProvenance(source=WriteSource.SYSTEM, written_at=_NOW),
                created_at=_NOW,
            ),
        )


def _seed_tasks(app_engine: Engine) -> None:
    store = TaskStore(app_engine)
    shapes = {
        "task-disclosed": ("Plan the dinner party menu", "conv-disclosed", ()),
        "task-fresh": ("Buy a gift for the host", "conv-fresh", ()),
        "task-run": ("Summarise the research", None, ("run-7",)),
        "task-clean": ("Book the EU-kontroll", "conv-clean", ()),
    }
    for task_id, (goal, conv, runs) in shapes.items():
        store.create(
            Task(
                id=task_id,
                owner_id=_OWNER,
                persona_id=_PERSONA,
                contract=Contract(goal=goal),
                state=TaskState.ACTIVE,
                conversation_id=conv,
                run_ids=runs,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )


def _seed(super_engine: Engine, app_engine: Engine | None = None) -> None:
    with super_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:u, :e)"),
            {"u": _OWNER, "e": f"{_OWNER}@x.test"},
        )
        conn.execute(
            text("INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :u, 'name: A')"),
            {"p": _PERSONA, "u": _OWNER},
        )
        _seed_conversations(conn)
    _seed_graph(super_engine)
    if app_engine is not None:
        _seed_tasks(app_engine)


def _store(app_engine: Engine) -> Any:  # noqa: ANN401
    return build_graph_store(engine=app_engine, embedder=_Emb(), audit_logger=MemoryAuditLogger())


def _as_owner(fn: Any) -> Any:  # noqa: ANN401
    token = current_user_id.set(_OWNER)
    try:
        return fn()
    finally:
        current_user_id.reset(token)


def _cite(kind: CitationKind, *refs: str) -> tuple[GroundingCitation, ...]:
    return tuple(GroundingCitation(kind=kind, ref=ref) for ref in refs)


def test_the_freshness_half_follows_the_synthesis_markers(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    _seed(migrated_engine)
    gate = ConversationWellbeingGate(_store(app_engine), app_engine)
    fresh = {"conv-fresh", "conv-behind", "conv-regen"}
    assert _as_owner(lambda: gate.unsynthesised(_OWNER, _ALL)) == fresh
    scan = _as_owner(lambda: gate.gated(_OWNER, _ALL, include_unsynthesised=True))
    assert scan == {"conv-disclosed"} | fresh
    event = _as_owner(lambda: gate.gated(_OWNER, _ALL, include_unsynthesised=False))
    assert event == {"conv-disclosed"}


def test_the_freshness_count_is_synthesis_own_count(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    """Pinned against synthesis itself: its repository and windowing say "new work" for
    exactly the conversations the gate calls unsynthesised (roles, superseded rows, no
    marker, no transcript at all), and after synthesis's own advance, none are."""
    _seed(migrated_engine)
    gate = ConversationWellbeingGate(_store(app_engine), app_engine)
    repo = PgSynthesisRepository()

    def _synthesis_has_work() -> set[str]:
        pending = set()
        with rls_connection(app_engine, _OWNER) as conn:
            for conv in _ALL:
                payload = make_conversation_synthesis_payload(
                    conversation_id=conv, persona_id=_PERSONA, message_count=0
                )
                data = repo.read(conn, owner_id=_OWNER, payload=payload)
                assert data is not None
                window = build_window(
                    messages=data.messages,
                    compacted_summary=data.compacted_summary,
                    synthesised_up_to=data.synthesised_up_to,
                    interaction_kind=InteractionKind.CONVERSATION,
                    interaction_id=conv,
                    persona_id=_PERSONA,
                )
                if window is not None:
                    pending.add(conv)
                    repo.advance(
                        conn,
                        owner_id=_OWNER,
                        payload=payload,
                        high_water_mark=window.high_water_mark,
                    )
        return pending

    before = _as_owner(lambda: gate.unsynthesised(_OWNER, _ALL))
    assert _synthesis_has_work() == before == {"conv-fresh", "conv-behind", "conv-regen"}
    assert _as_owner(lambda: gate.unsynthesised(_OWNER, _ALL)) == set()


def test_the_scan_graph_reader_drops_what_a_gated_conversation_taught(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    """HIGH-1: the untagged dinner-party node, and a node from an unsynthesised
    conversation, never reach the scan, as a pool node or as a lens target."""
    _seed(migrated_engine)
    store = _store(app_engine)
    reader = ApiScanGraphReader(store, ConversationWellbeingGate(store, app_engine))
    pool = _as_owner(lambda: reader.noticing_pool(_OWNER, limit=30))
    assert {n.id for n in pool} == {_PLAIN, _LOOSE}
    plain = next(n for n in pool if n.id == _PLAIN)
    assert [link.target_id for link in plain.links] == [_LOOSE]
    assert all("dinner" not in n.content for n in pool)


def test_the_scan_task_reader_drops_tasks_begun_in_a_gated_interaction(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    """MEDIUM-1: a task begun in the disclosure, in an unsynthesised conversation, or
    whose run taught a flagged node, is not scan material; the clean one is."""
    _seed(migrated_engine, app_engine)
    store = _store(app_engine)
    reader = ApiScanTaskReader(
        TaskStore(app_engine),
        CheckpointStore(app_engine),
        ConversationWellbeingGate(store, app_engine),
    )
    shown = _as_owner(lambda: reader.task_history(_OWNER, limit=10))
    assert {t.id for t in shown} == {"task-clean"}


def test_the_subject_check_judges_every_kind_by_its_source(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    _seed(migrated_engine, app_engine)
    check = ApiWellbeingSubjectCheck(_store(app_engine), app_engine)
    nodes = _cite(CitationKind.NODE, _DINNER, _PLAIN, _FRESH_NODE)
    convs = _cite(CitationKind.CONVERSATION, *_ALL)
    tasks = _cite(CitationKind.TASK, "task-disclosed", "task-fresh", "task-run", "task-clean")

    def _ask(citations: tuple[GroundingCitation, ...], scan: bool) -> set[str]:  # noqa: FBT001
        return _as_owner(lambda: check.tagged_refs(_OWNER, citations, include_unsynthesised=scan))

    assert _ask(nodes, True) == {_DINNER, _FRESH_NODE}
    assert _ask(nodes, False) == {_DINNER}
    assert _ask(convs, True) == {"conv-disclosed", "conv-fresh", "conv-behind", "conv-regen"}
    assert _ask(convs, False) == {"conv-disclosed"}
    assert _ask(tasks, True) == {"task-disclosed", "task-fresh", "task-run"}
    assert _ask(tasks, False) == {"task-disclosed", "task-run"}
    assert _ask(_cite(CitationKind.NODE, _FLAGGED), False) == {_FLAGGED}


def _pipeline(app_engine: Engine) -> InitiativePipeline:
    store = _store(app_engine)
    return InitiativePipeline(
        grounding=GroundingChecker(
            source=ApiGroundingSource(
                app_engine, TaskStore(app_engine), CheckpointStore(app_engine)
            ),
            backend=_YesJudge(),  # type: ignore[arg-type]
        ),
        wellbeing=ApiWellbeingSubjectCheck(store, app_engine),
        declines=DeclineStore(app_engine),
        ledger=LedgerAdapter(InitiativeLedger(app_engine)),
        users=ApiUserContextReader(app_engine, default_timezone="Europe/Oslo"),
        provenance=ApiProvenanceReader(store, app_engine),
        auditor=ApiPipelineAuditor(app_engine),
        dial_reader=lambda _o, _p: InitiativeDial.PROPOSE_ONLY,
        settings=InitiativeSettings(),
        delivery=None,
    )


@pytest.mark.parametrize(
    "citation",
    [
        GroundingCitation(kind=CitationKind.NODE, ref=_DINNER),  # the production shape
        GroundingCitation(kind=CitationKind.CONVERSATION, ref="conv-disclosed"),
    ],
    ids=["untagged-sibling-node", "conversation"],
)
def test_the_b3_shape_is_discarded_by_the_real_pipeline(
    migrated_engine: Engine, app_engine: Engine, citation: GroundingCitation
) -> None:
    """A candidate citing ONLY what the disclosure taught never becomes a notice."""
    _seed(migrated_engine)
    b3 = InitiativeCandidate(
        observation="The user has Mia's dinner party on Thursday.",
        citations=(citation,),
        trigger=InitiativeTrigger.APPROACHING_COMMITMENT,
        why_now="The dinner party is on your calendar.",
        plan=(PlannedStep(description="draft tips", categories=frozenset({ActionCategory.DRAFT})),),
        next_step="Draft a gift idea for the host.",
        value=0.8,
        acceptance=0.8,
        urgency=Urgency.BATCH,
        source=CandidateSource.SCAN,
        owner_id=_OWNER,
        persona_id=_PERSONA,
        prompt_version="a5-scan-v11",
        scanned_at=_NOW,
    )
    pipeline = _pipeline(app_engine)
    _as_owner(lambda: asyncio.run(pipeline.submit((b3,))))
    assert _as_owner(lambda: InitiativeLedger(app_engine).held_for_owner(_OWNER)) == []
    with migrated_engine.begin() as conn:
        actions = [
            r[0]
            for r in conn.execute(
                text(
                    "SELECT action FROM audit_log WHERE user_id = :u AND action LIKE 'initiative.%'"
                ),
                {"u": _OWNER},
            )
        ]
    assert "initiative.discard_wellbeing_subject" in actions


def test_a_conversation_disclosed_after_the_hold_never_flushes(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    """Review HIGH-A, the production sequence: a clean node is HELD; later its source
    conversation yields a tagged disclosure; the next flush expires the notice
    (``wellbeing_subject``, audited) instead of re-grounding and delivering it."""
    _seed(migrated_engine)
    held = InitiativeCandidate(
        observation="The user works as a teacher.",
        citations=_cite(CitationKind.NODE, _PLAIN),
        trigger=InitiativeTrigger.STALE_OPEN_LOOP,
        why_now="You mentioned wanting to plan the school term.",
        plan=(PlannedStep(description="draft", categories=frozenset({ActionCategory.DRAFT})),),
        next_step="draft a term plan for you to review",
        value=0.8,
        acceptance=0.8,
        urgency=Urgency.BATCH,
        source=CandidateSource.SCAN,
        owner_id=_OWNER,
        persona_id=_PERSONA,
        prompt_version="a5-scan-v11",
        scanned_at=_NOW,
    )
    pipeline = _pipeline(app_engine)
    ledger = InitiativeLedger(app_engine)
    _as_owner(lambda: asyncio.run(pipeline.submit((held,))))
    notices = _as_owner(lambda: ledger.held_for_owner(_OWNER))
    assert [n.candidate.anchor.ref for n in notices] == [_PLAIN]

    PostgresGraphBackend(engine=migrated_engine).insert_node_if_absent(
        _OWNER,
        _node(
            f"{_OWNER}::node::00000009",
            "The user disclosed a panic attack at school.",
            wellbeing="mental_health",
            source="conv-clean",
        ),
        [1.0] + [0.0] * (_DIM - 1),
    )
    _as_owner(lambda: asyncio.run(pipeline.flush(_OWNER)))

    assert _as_owner(lambda: ledger.held_for_owner(_OWNER)) == []
    after = _as_owner(lambda: ledger.get_notice(_OWNER, notices[0].id))
    assert after is not None
    assert after.disposition.value == "suppressed_stale"
    with migrated_engine.begin() as conn:
        reasons = [
            r[0]
            for r in conn.execute(
                text(
                    "SELECT metadata->>'reason' FROM audit_log WHERE user_id = :u "
                    "AND action = 'initiative.suppressed_stale'"
                ),
                {"u": _OWNER},
            )
        ]
    assert reasons == ["wellbeing_subject"]


def test_a7s_door_judges_a_task_grounding_by_its_origins(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    """Re-review LOW 3: a task-grounded event is dropped AT THE DOOR when the task was
    begun in a disclosing conversation or its run taught a flagged node, so no producer
    call is spent on an event the pipeline would discard. Disclosed-only, as before."""
    from persona_api.events.candidate_producer import ApiEventWellbeingCheck

    _seed(migrated_engine, app_engine)
    door = ApiEventWellbeingCheck(_store(app_engine), app_engine)

    def _gated(ref: str) -> bool:
        return bool(_as_owner(lambda: door.is_gated_subject(_OWNER, "task", ref)))

    assert _gated("task-disclosed")  # begun in the disclosing conversation
    assert _gated("task-run")  # its run taught a flagged node
    assert not _gated("task-fresh")  # freshness is never applied at the door
    assert not _gated("task-clean")
    without_engine = ApiEventWellbeingCheck(_store(app_engine))
    assert not _as_owner(lambda: without_engine.is_gated_subject(_OWNER, "task", "task-disclosed"))
