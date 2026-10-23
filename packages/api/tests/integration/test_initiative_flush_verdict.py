"""The flush keeps an admitted verdict unless what the judge read has changed (Postgres).

The A5-R-1 re-run (2026-10-23) lost notices at the next morning's flush because the
model judge, asked again about identical grounds, answered differently. A held notice
now carries the fingerprint of the excerpts it was admitted on: the flush re-runs the
mechanical layers and calls the judge again only when a cited excerpt has changed.
Proven here on the real ledger and the real grounding source: an unchanged node is
not re-judged (and the notice stays held for delivery), a changed node is re-judged
and, refused, the notice expires as ``grounding_dissolved``.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from persona.audit import MemoryAuditLogger
from persona.backends.types import ChatResponse, TokenUsage
from persona.graph import build_graph_store
from persona.graph.models import ConceptNode, NodeKind, NodeProvenance
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
from persona.schema.chunks import WriteSource
from persona.tools.categories import ActionCategory
from persona_api.initiative.pipeline_wiring import (
    ApiGroundingSource,
    ApiPipelineAuditor,
    ApiProvenanceReader,
    ApiUserContextReader,
    ApiWellbeingSubjectCheck,
    LedgerAdapter,
)
from persona_api.initiative.store import DeclineStore, InitiativeLedger, NoticeDisposition
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from persona_api.tasks.store import CheckpointStore, TaskStore
from persona_runtime.initiative import (
    INITIATIVE_SCAN_PROMPT_VERSION,
    GroundingChecker,
    InitiativePipeline,
)
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 9, 26, 5, 0, tzinfo=UTC)  # 07:00 Oslo
_FLUSH = datetime(2026, 9, 27, 5, 0, tzinfo=UTC)  # the next morning's fire
_OWNER = "own_flushverdict"
_PERSONA = "pers_flushverdict"
_NODE = f"{_OWNER}::node::00000001"
_CONTENT = "The user's car must pass its EU inspection by Monday 5 October 2026."
_DIM = 384


class _Emb:
    model_name = "fake"

    @property
    def dimension(self) -> int:
        return _DIM

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[1.0] + [0.0] * (_DIM - 1) for _ in texts]


class _CountingJudge:
    """Says YES (with a verbatim quote) while ``answer`` is True, NO after; counts calls."""

    provider_name = "scripted"
    model_name = "scripted"

    def __init__(self) -> None:
        self.answer = True
        self.calls = 0

    async def chat(self, messages: Any, **_: object) -> ChatResponse:  # noqa: ANN401
        self.calls += 1
        prompt = str(messages[-1].content)
        start = prompt.index("[excerpt 1]\n") + len("[excerpt 1]\n")
        quote = prompt[start : start + 30]
        content = json.dumps(
            {"entailed": True, "quote": quote} if self.answer else {"entailed": False}
        )
        return ChatResponse(
            content=content,
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


def _seed(super_engine: Engine) -> None:
    with super_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:u, :e)"),
            {"u": _OWNER, "e": f"{_OWNER}@x.test"},
        )
        conn.execute(
            text("INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :u, 'name: A')"),
            {"p": _PERSONA, "u": _OWNER},
        )
    PostgresGraphBackend(engine=super_engine).insert_node_if_absent(
        _OWNER, _node(_CONTENT), [1.0] + [0.0] * (_DIM - 1)
    )


def _node(content: str) -> ConceptNode:
    return ConceptNode(
        id=_NODE,
        node_kind=NodeKind.FACT,
        concept_name="inspection",
        content=content,
        provenance=(
            NodeProvenance(source=WriteSource.PERSONA_SELF, persona_id=_PERSONA, written_at=_NOW),
        ),
        created_at=_NOW,
    )


def _pipeline(app_engine: Engine, judge: _CountingJudge, now: list[datetime]) -> InitiativePipeline:
    store = build_graph_store(engine=app_engine, embedder=_Emb(), audit_logger=MemoryAuditLogger())
    return InitiativePipeline(
        grounding=GroundingChecker(
            source=ApiGroundingSource(
                app_engine, TaskStore(app_engine), CheckpointStore(app_engine)
            ),
            backend=judge,  # type: ignore[arg-type]
        ),
        wellbeing=ApiWellbeingSubjectCheck(store, app_engine),
        declines=DeclineStore(app_engine),
        ledger=LedgerAdapter(InitiativeLedger(app_engine)),
        users=ApiUserContextReader(app_engine, default_timezone="Europe/Oslo"),
        provenance=ApiProvenanceReader(store, app_engine),
        auditor=ApiPipelineAuditor(app_engine),
        dial_reader=lambda _o, _p: InitiativeDial.PROPOSE_ONLY,
        settings=InitiativeSettings(),
        delivery=None,  # staging posture: a survivor stays held, so the test reads its fate
        clock=lambda: now[0],
    )


_DATED = "Your car's EU inspection is due by Monday 5 October 2026."
_UNDATED = "Your car's EU inspection is coming up and no slot is booked."


def _candidate(why_now: str = _DATED) -> InitiativeCandidate:
    return InitiativeCandidate(
        observation="The car must pass its EU inspection by Monday 5 October 2026.",
        citations=(GroundingCitation(kind=CitationKind.NODE, ref=_NODE),),
        trigger=InitiativeTrigger.APPROACHING_COMMITMENT,
        why_now=why_now,
        plan=(
            PlannedStep(description="find a slot", categories=frozenset({ActionCategory.DRAFT})),
        ),
        next_step="find an inspection slot for you",
        value=0.8,
        acceptance=0.8,
        urgency=Urgency.BATCH,
        source=CandidateSource.SCAN,
        owner_id=_OWNER,
        persona_id=_PERSONA,
        prompt_version=INITIATIVE_SCAN_PROMPT_VERSION,
        scanned_at=_NOW,
    )


def _as_owner(fn: Any) -> Any:  # noqa: ANN401
    token = current_user_id.set(_OWNER)
    try:
        return fn()
    finally:
        current_user_id.reset(token)


def _held_and_submitted(
    app_engine: Engine, judge: _CountingJudge, now: list[datetime], why_now: str = _DATED
) -> Any:  # noqa: ANN401
    pipeline = _pipeline(app_engine, judge, now)
    _as_owner(lambda: asyncio.run(pipeline.submit((_candidate(why_now),))))
    held = _as_owner(lambda: InitiativeLedger(app_engine).held_for_owner(_OWNER))
    assert len(held) == 1
    assert held[0].candidate.grounding_fingerprint  # stored with the held notice
    assert held[0].candidate.grounding_served_model == "scripted/scripted"  # who admitted it
    assert judge.calls == 1
    return pipeline, held[0]


def test_an_unchanged_excerpt_is_not_judged_again_at_the_flush(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    _seed(migrated_engine)
    judge, now = _CountingJudge(), [_NOW]
    pipeline, notice = _held_and_submitted(app_engine, judge, now, _UNDATED)
    judge.answer = False  # it would now say no: it must not be asked
    now[0] = _FLUSH
    _as_owner(lambda: asyncio.run(pipeline.flush(_OWNER)))
    assert judge.calls == 1
    after = _as_owner(lambda: InitiativeLedger(app_engine).get_notice(_OWNER, notice.id))
    assert after is not None
    assert after.disposition is NoticeDisposition.HELD  # survived; no executor wired
    with migrated_engine.begin() as conn:  # the keep is audited (fifth review LOW-1)
        kept = conn.execute(
            text(
                "SELECT count(*) FROM audit_log WHERE user_id = :u "
                "AND action = 'initiative.grounding_kept'"
            ),
            {"u": _OWNER},
        ).scalar_one()
    assert kept == 1


def test_a_dated_message_is_judged_again_on_an_unchanged_excerpt(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    """Fourth review MEDIUM-1 (b): a message that names a date is re-read against the
    flush day's TODAY even though nothing it cites has changed."""
    _seed(migrated_engine)
    judge, now = _CountingJudge(), [_NOW]
    pipeline, notice = _held_and_submitted(app_engine, judge, now, _DATED)
    judge.answer = False
    now[0] = _FLUSH
    _as_owner(lambda: asyncio.run(pipeline.flush(_OWNER)))
    assert judge.calls == 2
    after = _as_owner(lambda: InitiativeLedger(app_engine).get_notice(_OWNER, notice.id))
    assert after is not None
    assert after.disposition.value == "suppressed_stale"


def test_a_changed_excerpt_is_judged_again_and_can_dissolve_the_notice(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    _seed(migrated_engine)
    judge, now = _CountingJudge(), [_NOW]
    pipeline, notice = _held_and_submitted(app_engine, judge, now, _UNDATED)
    # Same dates (so the mechanical specifics check passes), changed facts: only the
    # judge can tell, and it must be asked.
    changed = _node(
        "The user's car was due its EU inspection by Monday 5 October 2026; it passed early."
    )
    with migrated_engine.begin() as conn:  # the node's account changes (hash kept consistent)
        conn.execute(
            text("UPDATE graph_nodes SET content = :c, content_hash = :h WHERE id = :i"),
            {"c": changed.content, "h": changed.content_hash, "i": _NODE},
        )
    judge.answer = False
    now[0] = _FLUSH
    _as_owner(lambda: asyncio.run(pipeline.flush(_OWNER)))
    assert judge.calls == 2
    after = _as_owner(lambda: InitiativeLedger(app_engine).get_notice(_OWNER, notice.id))
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
    assert reasons == ["grounding_dissolved"]
