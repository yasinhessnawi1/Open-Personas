"""The WIRED consolidation pass — end-to-end through the A0 worker (Spec K7, T5).

Proves the LIVE chain, no hand-invoked pass in the live proof:

- **enqueue → real worker claim → ConsolidationHandler → pass.run → marker + audit** —
  a band-duplicate merges; a second triggered run reports ``merged=0`` (idempotent
  through the real chain, read from the persisted audit, not the unit path).
- **synthesis-tail trigger** — a real synthesis run enqueues a ``graph_consolidation``
  job (the composition-root wiring, not a constructed-in-isolation handler).
- **env-gating** — the handler is registered + the trigger bound ONLY when
  ``PERSONA_GRAPH_CONSOLIDATION_ENABLED`` is on (built-but-inert is the failure class).
- **§0 guardrail through the LIVE-triggered pass** — node rows monotone, content
  byte-equal, closed edges consolidation-tagged.
- **RLS chokepoint** — the pass under ``persona_app`` only ever touches the job owner
  (cross-tenant probe); an unset owner GUC fails closed.
"""

# ruff: noqa: ARG001, ARG002 — fixture-ordering params + fakes ignore protocol args.
from __future__ import annotations

import asyncio
import json
import math
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from persona.audit import JSONLAuditLogger
from persona.backends.types import ChatResponse, TokenUsage
from persona.graph import ConsolidationPass
from persona.graph.config import GraphSettings
from persona.graph.index import make_graph_index
from persona.graph.models import (
    ConceptNode,
    LinkType,
    NodeKind,
    NodeProvenance,
    TypedLink,
    make_edge_id,
)
from persona.graph.postgres import PostgresGraphBackend
from persona.jobs import JobRegistry
from persona.schema.chunks import WriteSource
from persona_api.background.worker_root import build_worker_registry
from persona_api.config import APIConfig
from persona_api.jobs import JobQueue, Worker
from persona_api.jobs.handlers.consolidation import (
    GRAPH_CONSOLIDATION_JOB_TYPE,
    ConsolidationJobPayload,
    enqueue_graph_consolidation,
    register_graph_consolidation_handler,
)
from persona_api.jobs.handlers.synthesis import SynthesisJobPayload, synthesis_idempotency_key
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from sqlalchemy import text

if TYPE_CHECKING:
    from pathlib import Path

    from persona.stores.embedder import Embedder
    from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

DIM = 384
_OWNER = "cons_user"
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _vec(primary: int, cos: float = 1.0, secondary: int = 383) -> list[float]:
    v = [0.0] * DIM
    v[primary] = cos
    v[secondary] = math.sqrt(max(0.0, 1.0 - cos * cos))
    return v


def _node(nid: str, name: str, *, ev: int = 1) -> ConceptNode:
    prov = tuple(
        NodeProvenance(source=WriteSource.PERSONA_SELF, written_at=T0 + timedelta(days=i))
        for i in range(ev)
    )
    return ConceptNode(
        id=nid,
        node_kind=NodeKind.CONCEPT,
        concept_name=name,
        content=f"content of {name}",
        provenance=prov,
        created_at=T0,
    )


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Engine:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set; skipping wired-consolidation test")
    return make_rls_engine(app_url.replace("+asyncpg", "+psycopg"))


@pytest.fixture
def owner_seeded(migrated_engine: Engine) -> Engine:
    with migrated_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:o, 'c@example.com')"), {"o": _OWNER}
        )
    return migrated_engine


def _seed_band_dup(engine: Engine, owner: str) -> None:
    """Seed a canonical + a true band-dup member + an external assertion target (superuser)."""
    backend = PostgresGraphBackend(engine=engine)
    c, m, x = f"{owner}::node::00000000", f"{owner}::node::00000001", f"{owner}::node::00000002"
    backend.insert_node(owner, _node(c, "Canonical", ev=2), _vec(0))
    backend.insert_node(owner, _node(m, "Member", ev=1), _vec(0, 0.85))
    backend.insert_node(owner, _node(x, "External"), _vec(9))
    backend.upsert_edge(
        owner,
        TypedLink(
            id=make_edge_id(c, m, LinkType.SEMANTIC),
            src_node_id=c,
            dst_node_id=m,
            link_type=LinkType.SEMANTIC,
            weight=0.85,
            created_at=T0,
            valid_at=T0,
        ),
    )
    backend.upsert_edge(
        owner,
        TypedLink(
            id=make_edge_id(m, x, LinkType.TEMPORAL),
            src_node_id=m,
            dst_node_id=x,
            link_type=LinkType.TEMPORAL,
            provenance=NodeProvenance(source=WriteSource.PERSONA_SELF, written_at=T0),
            created_at=T0,
            valid_at=T0,
        ),
    )


def _build_pass(app_engine: Engine, embedder: Embedder, audit_root: Path) -> ConsolidationPass:
    backend = PostgresGraphBackend(engine=app_engine)
    settings = GraphSettings()
    return ConsolidationPass(
        backend=backend,
        index=make_graph_index(
            settings=settings, engine=app_engine, float32_fetch=backend.embeddings_by_surrogate
        ),
        embedder=embedder,
        audit_logger=JSONLAuditLogger(audit_root),
        settings=settings,
    )


def _is_merged(engine: Engine, owner: str, node_id: str) -> bool:
    with engine.begin() as conn:
        val = conn.execute(
            text("SELECT merged_into FROM graph_nodes WHERE owner_id=:o AND id=:i"),
            {"o": owner, "i": node_id},
        ).scalar_one_or_none()
    return val is not None


def _audit_merged_counts(audit_root: Path) -> list[int]:
    counts: list[int] = []
    for f in sorted(audit_root.rglob("*.jsonl")):
        for line in f.read_text().splitlines():
            evt = json.loads(line)
            meta = evt.get("metadata") or {}
            if meta.get("action") == "consolidation_run":
                counts.append(int(meta["merged"]))
    return counts


C = f"{_OWNER}::node::00000000"
M = f"{_OWNER}::node::00000001"
X = f"{_OWNER}::node::00000002"


# ----- the LIVE chain: enqueue → worker → pass; second run merged=0 ---------


def test_consolidation_runs_through_the_worker_and_is_idempotent(
    owner_seeded: Engine, app_engine: Engine, embedder: Embedder, tmp_path: Path
) -> None:
    audit_root = tmp_path / "audit"
    _seed_band_dup(owner_seeded, _OWNER)
    registry = JobRegistry()
    register_graph_consolidation_handler(
        registry, pass_=_build_pass(app_engine, embedder, audit_root)
    )
    worker = Worker(
        dispatch_engine=owner_seeded, rls_engine=app_engine, registry=registry, worker_id="w-cons"
    )
    queue = JobQueue(owner_seeded)

    assert not _is_merged(owner_seeded, _OWNER, M)
    enqueue_graph_consolidation(queue, owner_id=_OWNER, delay_seconds=0.0, bucket_seconds=1.0)
    assert asyncio.run(worker.run_once()) == 1  # claim → handler → pass.run
    assert _is_merged(owner_seeded, _OWNER, M)  # the band-dup merged through the chain

    # a SECOND trigger (a distinct coalescing window → a fresh, claimable job): nothing
    # new merges — idempotent through the real chain.
    queue.enqueue(
        type=GRAPH_CONSOLIDATION_JOB_TYPE,
        owner_id=_OWNER,
        payload=ConsolidationJobPayload(watermark_bucket="second").model_dump(),
        idempotency_key="graph_consolidation:second",
    )
    assert asyncio.run(worker.run_once()) == 1  # the second run claimed + executed
    # the persisted audit records both runs honestly: first merged 1, second merged 0.
    assert _audit_merged_counts(audit_root) == [1, 0]


# ----- §0 guardrail holds through the LIVE-triggered pass -------------------


def test_guardrail_holds_through_worker_triggered_pass(
    owner_seeded: Engine, app_engine: Engine, embedder: Embedder, tmp_path: Path
) -> None:
    _seed_band_dup(owner_seeded, _OWNER)
    registry = JobRegistry()
    register_graph_consolidation_handler(
        registry, pass_=_build_pass(app_engine, embedder, tmp_path / "audit")
    )
    worker = Worker(
        dispatch_engine=owner_seeded, rls_engine=app_engine, registry=registry, worker_id="w-g"
    )
    with owner_seeded.begin() as conn:
        nodes_before = conn.execute(text("SELECT count(*) FROM graph_nodes")).scalar_one()
        content_before = dict(conn.execute(text("SELECT id, content FROM graph_nodes")).all())
    enqueue_graph_consolidation(
        JobQueue(owner_seeded), owner_id=_OWNER, delay_seconds=0.0, bucket_seconds=1.0
    )
    assert asyncio.run(worker.run_once()) == 1

    with owner_seeded.begin() as conn:
        nodes_after = conn.execute(text("SELECT count(*) FROM graph_nodes")).scalar_one()
        content_after = dict(conn.execute(text("SELECT id, content FROM graph_nodes")).all())
        untagged_closed = conn.execute(
            text(
                "SELECT count(*) FROM graph_edges WHERE invalid_at IS NOT NULL "
                "AND invalidated_by NOT LIKE 'consolidation:%'"
            )
        ).scalar_one()
    assert nodes_after == nodes_before  # no hard-delete
    assert content_after == content_before  # content byte-equal (no LLM rewrite)
    assert untagged_closed == 0  # only consolidation-tagged closes (K7-D-1.3 stays green)


# ----- RLS chokepoint: owner-scoped + fail-closed under persona_app ---------


def test_pass_under_persona_app_is_owner_scoped_and_fails_closed(
    owner_seeded: Engine, app_engine: Engine, embedder: Embedder, tmp_path: Path
) -> None:
    # seed TWO tenants (superuser); the pass for u1 must never touch u2.
    with owner_seeded.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email) VALUES ('u_other', 'o@example.com')"))
    _seed_band_dup(owner_seeded, _OWNER)
    _seed_band_dup(owner_seeded, "u_other")
    pass_ = _build_pass(app_engine, embedder, tmp_path / "audit")

    # bind the owner GUC as the worker's executor does, run under persona_app RLS.
    token = current_user_id.set(_OWNER)
    try:
        report = pass_.run(_OWNER)
    finally:
        current_user_id.reset(token)
    assert report.nodes_merged == 1
    # u_other untouched (owner-scoped); non-vacuous — it WOULD merge if scanned.
    assert not _is_merged(owner_seeded, "u_other", "u_other::node::00000001")

    # fail-closed: with NO owner GUC bound, the RLS-scoped candidate scan yields nothing
    # (the persona_app checkout listener sets an empty GUC → current_setting IS NULL →
    # matches no policy row). The pass can read no candidate under an unbound owner.
    scoped_backend = PostgresGraphBackend(engine=app_engine)
    assert scoped_backend.dirty_candidates(_OWNER, None) == []


# ----- composition-root wiring, env-gated (bar 3) --------------------------


class _CheapBackend:
    """A one-candidate chat backend so synthesis writes exactly one graph node."""

    @property
    def provider_name(self) -> str:
        return "anthropic"

    @property
    def model_name(self) -> str:
        return "claude-haiku-4-5-20251001"

    @property
    def supports_native_tools(self) -> bool:
        return False

    @property
    def supports_vision(self) -> bool:
        return False

    async def chat(self, messages: object, **kwargs: object) -> ChatResponse:
        content = (
            '{"candidates": [{"concept_name": "vegetarian", "content": "is vegetarian",'
            ' "node_kind": "preference", "evidence_span": "I went vegetarian"}]}'
        )
        return ChatResponse(
            content=content,
            tool_calls=[],
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            model=self.model_name,
            provider=self.provider_name,
            latency_ms=0.0,
        )

    def chat_stream(self, *a: object, **k: object) -> object:
        raise NotImplementedError


class _FakeTierRegistry:
    def get(self, tier: str) -> _CheapBackend:
        return _CheapBackend()


def _registry(app_engine: Engine, embedder: Embedder, audit_root: Path) -> JobRegistry:
    return build_worker_registry(
        rls_engine=app_engine,
        embedder=embedder,
        tier_registry=_FakeTierRegistry(),
        # Post-R5 signature: the worker derives its audit sink from config
        # (JSONL at audit_root under the default backend — byte-equivalent).
        config=APIConfig(audit_root=str(audit_root)),
        synthesis_tier="small",
    )


def test_consolidation_handler_registered_only_when_enabled(
    app_engine: Engine, embedder: Embedder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PERSONA_GRAPH_CONSOLIDATION_ENABLED", "true")
    enabled = _registry(app_engine, embedder, tmp_path / "a1")
    assert GRAPH_CONSOLIDATION_JOB_TYPE in enabled.types()

    monkeypatch.setenv("PERSONA_GRAPH_CONSOLIDATION_ENABLED", "false")
    disabled = _registry(app_engine, embedder, tmp_path / "a2")
    assert GRAPH_CONSOLIDATION_JOB_TYPE not in disabled.types()  # built-but-inert avoided


def test_synthesis_tail_enqueues_consolidation_through_the_root(
    migrated_engine: Engine,
    app_engine: Engine,
    embedder: Embedder,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PERSONA_GRAPH_CONSOLIDATION_ENABLED", "true")
    _owner, _persona, _convo = "tail_user", "tail_persona", "tail_convo"
    with migrated_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:o, 't@example.com')"), {"o": _owner}
        )
        conn.execute(
            text(
                "INSERT INTO personas (id, owner_id, yaml) "
                "VALUES (:p, :o, 'schema_version: \"1.0\"')"
            ),
            {"p": _persona, "o": _owner},
        )
        conn.execute(
            text(
                "INSERT INTO conversations (id, owner_id, persona_id, compacted_summary) "
                "VALUES (:c, :o, :p, '')"
            ),
            {"c": _convo, "o": _owner, "p": _persona},
        )
        for role, content in (("user", "I went vegetarian"), ("assistant", "Noted.")):
            conn.execute(
                text("INSERT INTO messages (conversation_id, role, content) VALUES (:c, :r, :t)"),
                {"c": _convo, "r": role, "t": content},
            )
    registry = _registry(app_engine, embedder, tmp_path / "audit")
    worker = Worker(
        dispatch_engine=migrated_engine, rls_engine=app_engine, registry=registry, worker_id="w-t"
    )
    payload = SynthesisJobPayload(
        interaction_kind="conversation",
        interaction_id=_convo,
        persona_id=_persona,
        high_water_mark=2,
    )
    JobQueue(migrated_engine).enqueue(
        type="synthesis",
        owner_id=_owner,
        payload=payload.model_dump(),
        idempotency_key=synthesis_idempotency_key(payload),
    )
    # run synthesis: it writes a graph node AND enqueues a consolidation job at its tail.
    assert asyncio.run(worker.run_once()) == 1
    with migrated_engine.begin() as conn:
        jobs = conn.execute(
            text("SELECT count(*) FROM jobs WHERE type = :t AND owner_id = :o"),
            {"t": GRAPH_CONSOLIDATION_JOB_TYPE, "o": _owner},
        ).scalar_one()
    assert jobs == 1  # the synthesis tail enqueued consolidation (K7-D-5, via the real root)


# ----- per-cluster failure isolation, honest report (bar 2) ----------------


class _PoisonBackend(PostgresGraphBackend):
    """Injects a failure when a specific member's edges are closed (mid-merge)."""

    def __init__(self, *, engine: Engine, poison_member: str) -> None:
        super().__init__(engine=engine)
        self._poison = poison_member

    def close_edges_incident(
        self, owner_id: str, node_id: str, *, invalidated_by: str
    ) -> list[str]:
        if node_id == self._poison:
            raise RuntimeError("injected merge failure")
        return super().close_edges_incident(owner_id, node_id, invalidated_by=invalidated_by)


def test_one_cluster_failure_does_not_abort_the_run(
    owner_seeded: Engine, app_engine: Engine, embedder: Embedder, tmp_path: Path
) -> None:
    # two independent clusters A(00,01) + B(10,11); poison member 01 → cluster A fails,
    # cluster B still merges, and the report records A's failure honestly.
    backend = PostgresGraphBackend(engine=owner_seeded)  # superuser seed
    a_c, a_m = f"{_OWNER}::node::00000000", f"{_OWNER}::node::00000001"
    b_c, b_m = f"{_OWNER}::node::00000010", f"{_OWNER}::node::00000011"
    backend.insert_node(_OWNER, _node(a_c, "A-can", ev=2), _vec(0))
    backend.insert_node(_OWNER, _node(a_m, "A-mem"), _vec(0, 0.85))
    backend.insert_node(_OWNER, _node(b_c, "B-can", ev=2), _vec(40))
    backend.insert_node(_OWNER, _node(b_m, "B-mem"), _vec(40, 0.85))
    backend.upsert_edge(
        _OWNER,
        TypedLink(
            id=make_edge_id(a_c, a_m, LinkType.SEMANTIC),
            src_node_id=a_c,
            dst_node_id=a_m,
            link_type=LinkType.SEMANTIC,
            weight=0.85,
            created_at=T0,
            valid_at=T0,
        ),
    )
    backend.upsert_edge(
        _OWNER,
        TypedLink(
            id=make_edge_id(b_c, b_m, LinkType.SEMANTIC),
            src_node_id=b_c,
            dst_node_id=b_m,
            link_type=LinkType.SEMANTIC,
            weight=0.85,
            created_at=T0,
            valid_at=T0,
        ),
    )

    poison = _PoisonBackend(engine=app_engine, poison_member=a_m)
    settings = GraphSettings()
    pass_ = ConsolidationPass(
        backend=poison,
        index=make_graph_index(
            settings=settings, engine=app_engine, float32_fetch=poison.embeddings_by_surrogate
        ),
        embedder=embedder,
        audit_logger=JSONLAuditLogger(tmp_path / "audit"),
        settings=settings,
    )
    token = current_user_id.set(_OWNER)
    try:
        report = pass_.run(_OWNER)
    finally:
        current_user_id.reset(token)

    # cluster B merged; cluster A recorded as a failure (not silent) — the run went on.
    assert b_m in {m for g in report.merges for m in g.member_ids}
    assert _is_merged(owner_seeded, _OWNER, b_m)  # B merged through
    assert any(s.node_id == a_c and "merge failed" in s.reason for s in report.skipped)
    assert not _is_merged(owner_seeded, _OWNER, a_m)  # A's member never completed the merge
