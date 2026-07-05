"""A5 T7 — the mixed batch through the REAL pipeline + stores (the T7 gate case).

Real Postgres under ``persona_app``: real T3 stores (declines + ledger), the
real T4 checker over the real ``ApiGroundingSource`` (only the entailment
judge scripted — it answers YES with a verbatim quote, so ADMISSION is decided
by the real mechanics), the real wellbeing/user-context/provenance/auditor
adapters. Six candidates in, EXACTLY one survivor out, every non-survivor's
disposition audited:

  1. grounded-good            → the ONE held notice (voiced per A5-D-6)
  2. ungrounded (missing ref) → audited ``discard_ungrounded``
  3. wellbeing-cited          → audited ``discard_wellbeing_subject`` (criterion 6)
  4. low-value                → audited ``discard_low_value``
  5. duplicate (same key, another persona) → the store's ``duplicate_suppressed``
  6. pre-declined             → audited ``discard_declined``

Then the flush legs on the survivor's world: grounding dissolves (the cited
node is deleted) → the held notice EXPIRES as ``suppressed_stale`` (T3 ruling 3
+ the T7 re-grounding bar), never delivered.
"""

# ruff: noqa: ARG001, ARG002 — fixture params + fakes ignore some args
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
from persona_api.initiative.store import (
    DeclineSource,
    DeclineStore,
    InitiativeLedger,
    NoticeDisposition,
)
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from persona_api.tasks.store import CheckpointStore, TaskStore
from persona_runtime.initiative import GroundingChecker, InitiativePipeline
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Iterator

    from persona.initiative import InitiativeDial
    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 7, 4, 6, 0, tzinfo=UTC)
_OWNER = "own_a5mb"
_GOOD_NODE = f"{_OWNER}::node::good0001"
_TAGGED_NODE = f"{_OWNER}::node::tagged01"
_DECLINED_NODE = f"{_OWNER}::node::declined1"
_HEARING = "The custody hearing is on Friday 10 July."
DIM = 384


class _VerbatimYesJudge:
    """Scripted entailment judge: YES with a quote copied from the real content.

    Kept verbatim-honest so the T4 substring check passes — ADMISSION is still
    decided by the real resolution mechanics (a missing/tagged/deleted node
    fails regardless of what this judge says).
    """

    provider_name = "anthropic"
    model_name = "scripted"

    async def chat(self, messages: Any, **_: object) -> ChatResponse:  # noqa: ANN401
        prompt = str(messages[-1].content)
        marker = "[excerpt 1]\n"
        start = prompt.index(marker) + len(marker)
        quote = prompt[start : start + 25]  # verbatim from whatever content resolved
        return ChatResponse(
            content=json.dumps({"entailed": True, "quote": quote}),
            model=self.model_name,
            provider=self.provider_name,
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            latency_ms=1.0,
        )


class _Emb:
    model_name = "fake"

    @property
    def dimension(self) -> int:
        return DIM

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[1.0] + [0.0] * (DIM - 1) for _ in texts]


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Iterator[Engine]:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL (non-superuser role) not set; skipping RLS test")
    engine = make_rls_engine(app_url.replace("+asyncpg", "+psycopg"))
    yield engine
    engine.dispose()


def _seed(migrated_engine: Engine) -> None:
    with migrated_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:u, :e) ON CONFLICT DO NOTHING"),
            {"u": _OWNER, "e": f"{_OWNER}@x.test"},
        )
        for pid in ("pers_mb_a", "pers_mb_b"):
            conn.execute(
                text(
                    "INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :u, 'name: A') "
                    "ON CONFLICT DO NOTHING"
                ),
                {"p": pid, "u": _OWNER},
            )
    backend = PostgresGraphBackend(engine=migrated_engine)
    backend.insert_node_if_absent(
        _OWNER,
        ConceptNode(
            id=_GOOD_NODE,
            node_kind=NodeKind.FACT,
            concept_name="hearing",
            content=_HEARING,
            provenance=(
                NodeProvenance(
                    source=WriteSource.PERSONA_SELF, persona_id="pers_mb_a", written_at=_NOW
                ),
            ),
            created_at=_NOW,
        ),
        [1.0] + [0.0] * (DIM - 1),
    )
    backend.insert_node_if_absent(
        _OWNER,
        ConceptNode(
            id=_DECLINED_NODE,
            node_kind=NodeKind.FACT,
            concept_name="declined topic",
            content=_HEARING,  # resolvable + entailed — the DECLINE gate must do the work
            provenance=(NodeProvenance(source=WriteSource.SYSTEM, written_at=_NOW),),
            created_at=_NOW,
        ),
        [1.0] + [0.0] * (DIM - 1),
    )
    backend.insert_node_if_absent(
        _OWNER,
        ConceptNode(
            id=_TAGGED_NODE,
            node_kind=NodeKind.CIRCUMSTANCE,
            concept_name="struggle",
            content="The user disclosed a personal struggle.",
            wellbeing_category="self_harm",
            provenance=(NodeProvenance(source=WriteSource.SYSTEM, written_at=_NOW),),
            created_at=_NOW,
        ),
        [1.0] + [0.0] * (DIM - 1),
    )


def _candidate(ref: str, **overrides: Any) -> InitiativeCandidate:  # noqa: ANN401
    fields: dict[str, Any] = {
        "observation": "The hearing is Friday and nothing is drafted.",
        "citations": (GroundingCitation(kind=CitationKind.NODE, ref=ref),),
        "trigger": InitiativeTrigger.APPROACHING_COMMITMENT,
        "why_now": "The date entered the horizon.",
        "plan": (PlannedStep(description="draft", categories=frozenset({ActionCategory.DRAFT})),),
        "next_step": "Draft the letter.",
        "value": 0.9,
        "acceptance": 0.8,
        "urgency": Urgency.BATCH,
        "source": CandidateSource.SCAN,
        "owner_id": _OWNER,
        "persona_id": "pers_mb_a",
        "prompt_version": "a5-scan-v1",
        "scanned_at": _NOW,
    }
    fields.update(overrides)
    return InitiativeCandidate(**fields)


def _pipeline(app_engine: Engine) -> InitiativePipeline:
    graph_store = build_graph_store(
        engine=app_engine, embedder=_Emb(), audit_logger=MemoryAuditLogger()
    )
    settings = InitiativeSettings()

    def _dial(_owner: str, _persona: str) -> InitiativeDial:
        from persona.initiative import InitiativeDial as Dial

        return Dial.PROPOSE_ONLY

    return InitiativePipeline(
        grounding=GroundingChecker(
            source=ApiGroundingSource(
                app_engine, TaskStore(app_engine), CheckpointStore(app_engine)
            ),
            backend=_VerbatimYesJudge(),  # type: ignore[arg-type] — the ONLY scripted piece
        ),
        wellbeing=ApiWellbeingSubjectCheck(graph_store),
        declines=DeclineStore(app_engine),
        ledger=LedgerAdapter(InitiativeLedger(app_engine)),
        users=ApiUserContextReader(app_engine, default_timezone="Europe/Oslo"),
        provenance=ApiProvenanceReader(graph_store, app_engine),
        auditor=ApiPipelineAuditor(app_engine),
        dial_reader=_dial,
        settings=settings,
    )


def _audit_count(engine: Engine, action: str) -> int:
    with engine.begin() as conn:
        return conn.execute(
            text("SELECT count(*) FROM audit_log WHERE user_id = :u AND action = :a"),
            {"u": _OWNER, "a": action},
        ).scalar_one()


def test_mixed_batch_yields_exactly_the_right_survivor_with_audited_dispositions(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    _seed(migrated_engine)
    pipeline = _pipeline(app_engine)
    ledger = InitiativeLedger(app_engine)
    declines = DeclineStore(app_engine)

    # Pre-decline candidate 6's opportunity (a different anchor node).
    declined_key = _candidate(_DECLINED_NODE).opportunity_key
    assert (
        declines.record_decline(
            _OWNER,
            opportunity_key=declined_key,
            trigger="approaching_commitment",
            source=DeclineSource.DECLINED_REPLY,
            persona_id=None,
            now=_NOW,
        )
        is not None
    )

    batch = (
        _candidate(_GOOD_NODE),  # 1 — the survivor
        _candidate(f"{_OWNER}::node::missing"),  # 2 — unresolvable citation
        _candidate(_TAGGED_NODE),  # 3 — wellbeing-tagged subject
        _candidate(_GOOD_NODE, value=0.1),  # 4 — below the value threshold
        _candidate(_GOOD_NODE, persona_id="pers_mb_b"),  # 5 — duplicate opportunity
        _candidate(_DECLINED_NODE),  # 6 — live-declined topic
    )
    token = current_user_id.set(_OWNER)
    try:
        asyncio.run(pipeline.submit(batch))

        held = ledger.held_for_owner(_OWNER)
    finally:
        current_user_id.reset(token)

    # EXACTLY one survivor: the grounded, admissible, novel, wanted one — held
    # (batch urgency; no delivery executor until T8/T9), voiced by the persona
    # whose provenance grounds it (A5-D-6).
    assert len(held) == 1
    assert held[0].opportunity_key == _candidate(_GOOD_NODE).opportunity_key
    assert held[0].persona_id == "pers_mb_a"
    assert held[0].disposition is NoticeDisposition.HELD

    # Every non-survivor left its audited disposition — no silent skips.
    # (2) resolves nothing: the missing node discards BEFORE the judge runs.
    assert _audit_count(migrated_engine, "initiative.discard_ungrounded") == 1
    # (3) the criterion-6 automatic-fail guard, from the pipeline side.
    assert _audit_count(migrated_engine, "initiative.discard_wellbeing_subject") == 1
    # (4) below the value threshold — silently-not-raised, but AUDITED.
    assert _audit_count(migrated_engine, "initiative.discard_low_value") == 1
    # (5) one user-level notice per opportunity: the duplicate no-ops, audited by the store.
    assert _audit_count(migrated_engine, "initiative.duplicate_suppressed") == 1
    # (6) dismissal is durable communication (A5-D-4).
    assert _audit_count(migrated_engine, "initiative.discard_declined") == 1


def test_flush_expires_a_held_notice_whose_grounding_dissolved(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    """The T7 re-grounding bar on the REAL stack: delete the cited node between
    hold and flush → the notice EXPIRES as suppressed_stale (audited), never delivers."""
    _seed(migrated_engine)
    pipeline = _pipeline(app_engine)
    ledger = InitiativeLedger(app_engine)

    token = current_user_id.set(_OWNER)
    try:
        if not ledger.held_for_owner(_OWNER):
            asyncio.run(pipeline.submit((_candidate(_GOOD_NODE),)))
        assert len(ledger.held_for_owner(_OWNER)) == 1

        # The graph moves on: the cited node is deleted (K5-style user removal).
        with migrated_engine.begin() as conn:
            conn.execute(text("DELETE FROM graph_nodes WHERE id = :i"), {"i": _GOOD_NODE})

        asyncio.run(pipeline.flush(_OWNER))
        assert ledger.held_for_owner(_OWNER) == []
    finally:
        current_user_id.reset(token)
    assert _audit_count(migrated_engine, "initiative.suppressed_stale") >= 1
    with migrated_engine.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": _OWNER})
