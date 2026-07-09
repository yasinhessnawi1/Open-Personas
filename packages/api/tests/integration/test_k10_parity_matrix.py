"""K10 parity matrix — memory features × edition, PROVEN not asserted (Spec K10, T5).

K10-R-C: parity must be *proven*. This module drives the REAL background passes
through the REAL composition root (``build_worker_registry`` + ``Worker.run_once``)
against a REAL community-managed embedded Postgres (provisioned by
``CommunityDbManager``, exactly the T1/T2 boot path), and asserts each worker-driven
memory feature RUNS and produces output — because "full parity" means the
consolidation/gist passes actually fire, not merely that the code path is reachable.

## Feature × edition matrix  (managed = K10, sqlite = legacy D-5 deprecated)

| Memory feature                    | cloud | community-managed  | community-sqlite    |
|-----------------------------------|:-----:|:------------------:|:-------------------:|
| Typed memory (4 stores) + episodic| GREEN | GREEN (PostgresBE) | GREEN (Chroma model)|
| Knowledge graph write             | GREEN | GREEN (this file)  | DARK (graph_* gone) |
| K2 synthesis (extract → graph)    | GREEN | GREEN (this file)  | DARK (worker refuse)|
| K7 consolidation (dedup/merge)    | GREEN | GREEN (this file)  | DARK (worker refuse)|
| K8 gist/tiering (episodic → gist) | GREEN | GREEN (this file)  | DARK (worker refuse)|
| Worker composition (all tenants)  | GREEN | GREEN (this file)  | REFUSED (T3 guard)  |
| Memory HTTP routes (available)    | GREEN | GREEN (auto on)    | DARK (available=F)  |

The community-MANAGED column is proven GREEN below for the worker-driven passes
(synthesis, consolidation, K8 gist). The legacy-sqlite column's honest DARK cells
(what D-5 deprecation gives up) are pinned in ``test_legacy_sqlite_*`` — no embedded
Postgres needed there; they document the cost of the deprecated path.

Cloud is unchanged and already proven by the shipped ``*_wired`` integration tests
(this file deliberately reuses their exact harness so the parity is like-for-like).
"""

# ruff: noqa: ARG002 — fakes ignore protocol args.
from __future__ import annotations

import asyncio
import math
import os
import shutil
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from persona.backends.types import ChatResponse, TokenUsage
from persona.schema.chunks import ChunkProvenance, PersonaChunk, WriteSource, mint_chunk_id
from persona.stores.postgres import PostgresBackend
from persona_api.background.worker_root import build_worker_registry
from persona_api.config import APIConfig
from persona_api.db.community_managed import CommunityDbManager, run_migrations
from persona_api.jobs import JobQueue, Worker
from persona_api.jobs.handlers.consolidation import (
    GRAPH_CONSOLIDATION_JOB_TYPE,
    enqueue_graph_consolidation,
)
from persona_api.jobs.handlers.episodic_consolidation import EPISODIC_CONSOLIDATION_JOB_TYPE
from persona_api.jobs.handlers.synthesis import SynthesisJobPayload, synthesis_idempotency_key
from persona_api.middleware.rls_context import make_rls_engine
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.integration

pytest.importorskip("pixeltable_pgserver")

if TYPE_CHECKING:
    from collections.abc import Iterator

    from persona.stores.embedder import Embedder
    from sqlalchemy.engine import Engine
    from tests.conftest import HashEmbedder384


# ---- deterministic fakes (the wired-test convention: real plumbing, fake model) ----


class _CheapBackend:
    """A one-candidate extractor / canned summarizer — real passes, no model key."""

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
        # Synthesis reads a candidates JSON; the K8 summarizer reads free text. This
        # candidates payload doubles as a non-empty gist body for the summarizer.
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


# ---- the community-managed embedded Postgres (the real T1/T2 boot substrate) ----


@pytest.fixture(scope="module")
def managed_url() -> Iterator[str]:
    """Provision ONE embedded managed Postgres, migrate it, yield its DSN.

    Module-scoped so the ~one-off provision + 38-migration cost is paid once; each
    pass test uses a distinct owner to stay isolated on the shared instance.
    """
    base = tempfile.mkdtemp(prefix="k10pm", dir="/tmp")  # short → under the socket cap
    prior_db_url = os.environ.get("DATABASE_URL")
    mgr = CommunityDbManager(external_url=None, base_dir=Path(base))
    try:
        url = mgr.start()
        run_migrations(url)  # the SAME Alembic chain as cloud (D-K10-8)
        yield url
    finally:
        mgr.stop()
        # run_migrations mutates DATABASE_URL (D-K10-8) — restore the prior value.
        if prior_db_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prior_db_url
        shutil.rmtree(base, ignore_errors=True)


@pytest.fixture
def rls_engine(managed_url: str) -> Iterator[Engine]:
    # The community single-owner superuser engine (D-K10-2/-6): make_rls_engine so
    # the checkout listener binds the per-job owner GUC exactly as production does
    # (inert under the superuser, but the same wiring). Serves as BOTH the worker's
    # dispatch and rls engine — community is single-owner, one engine.
    engine = make_rls_engine(managed_url)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def seed_engine(managed_url: str) -> Iterator[Engine]:
    # A plain superuser engine for seeding + assertions (RLS bypassed → no GUC dance).
    engine = create_engine(managed_url)
    try:
        yield engine
    finally:
        engine.dispose()


def _registry(rls_engine: Engine, embedder: Embedder, audit_root: str) -> object:
    return build_worker_registry(
        rls_engine=rls_engine,
        embedder=embedder,
        tier_registry=_FakeTierRegistry(),  # type: ignore[arg-type]
        config=APIConfig(audit_root=audit_root),
        synthesis_tier="small",
        memory_backend=PostgresBackend(engine=rls_engine, embedder=embedder),
    )


def _drain(worker: Worker, *, max_jobs: int = 6) -> int:
    ran = 0
    for _ in range(max_jobs):
        if asyncio.run(worker.run_once()) != 1:
            break
        ran += 1
    return ran


# ---- community-MANAGED column: the worker-driven passes RUN + produce output ----


def test_managed_worker_registers_all_memory_tenants(
    rls_engine: Engine, embedder: HashEmbedder384, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Composition parity: the real root registers synthesis + K7 + K8 on managed PG."""
    monkeypatch.setenv("PERSONA_GRAPH_CONSOLIDATION_ENABLED", "true")
    monkeypatch.setenv("PERSONA_EPISODIC_ENGINE_ENABLED", "true")
    registry = _registry(rls_engine, embedder, str(tmp_path))
    types = registry.types()  # type: ignore[attr-defined]
    assert "synthesis" in types
    assert GRAPH_CONSOLIDATION_JOB_TYPE in types
    assert EPISODIC_CONSOLIDATION_JOB_TYPE in types


def test_managed_synthesis_runs_and_writes_graph(
    rls_engine: Engine, seed_engine: Engine, embedder: HashEmbedder384, tmp_path: Path
) -> None:
    """K2 synthesis: real worker claim → extract → graph node (the pass RAN + output)."""
    owner, persona, convo = "pm_synth_owner", "pm_synth_persona", "pm_synth_convo"
    with seed_engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email) VALUES (:o, 's@ex.com')"), {"o": owner})
        conn.execute(
            text("INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :o, 'name: s')"),
            {"p": persona, "o": owner},
        )
        conn.execute(
            text(
                "INSERT INTO conversations (id, owner_id, persona_id, compacted_summary) "
                "VALUES (:c, :o, :p, '')"
            ),
            {"c": convo, "o": owner, "p": persona},
        )
        for role, content in (("user", "I went vegetarian"), ("assistant", "Noted.")):
            conn.execute(
                text("INSERT INTO messages (conversation_id, role, content) VALUES (:c, :r, :t)"),
                {"c": convo, "r": role, "t": content},
            )

    registry = _registry(rls_engine, embedder, str(tmp_path))
    worker = Worker(
        dispatch_engine=rls_engine, rls_engine=rls_engine, registry=registry, worker_id="pm-synth"
    )
    payload = SynthesisJobPayload(
        interaction_kind="conversation",
        interaction_id=convo,
        persona_id=persona,
        high_water_mark=2,
    )
    JobQueue(rls_engine).enqueue(
        type="synthesis",
        owner_id=owner,
        payload=payload.model_dump(),
        idempotency_key=synthesis_idempotency_key(payload),
    )
    assert _drain(worker) >= 1  # the worker CLAIMED and ran — nothing hand-invoked

    with seed_engine.begin() as conn:
        content = conn.execute(
            text("SELECT content FROM graph_nodes WHERE owner_id = :o"), {"o": owner}
        ).scalar_one()
    assert content == "is vegetarian"  # K2 synthesis produced a real graph node


def test_managed_k7_consolidation_runs_and_merges(
    rls_engine: Engine, seed_engine: Engine, embedder: HashEmbedder384, tmp_path: Path
) -> None:
    """K7 consolidation: real worker claim → pass.run → a band-duplicate MERGES."""
    from persona.graph.models import (
        LinkType,
        NodeKind,
        NodeProvenance,
        TypedLink,
        make_edge_id,
    )
    from persona.graph.postgres import PostgresGraphBackend

    owner = "pm_cons_owner"
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    with seed_engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email) VALUES (:o, 'c@ex.com')"), {"o": owner})

    # Seed a canonical + a true band-dup member sharing a near-identical embedding.
    dim = 384

    def _vec(primary: int, cos: float = 1.0) -> list[float]:
        v = [0.0] * dim
        v[primary] = cos
        v[383] = math.sqrt(max(0.0, 1.0 - cos * cos))
        return v

    def _node(nid: str, name: str, ev: int) -> object:
        from persona.graph.models import ConceptNode

        prov = tuple(
            NodeProvenance(source=WriteSource.PERSONA_SELF, written_at=t0 + timedelta(days=i))
            for i in range(ev)
        )
        return ConceptNode(
            id=nid,
            node_kind=NodeKind.CONCEPT,
            concept_name=name,
            content=f"content of {name}",
            provenance=prov,
            created_at=t0,
        )

    backend = PostgresGraphBackend(engine=seed_engine)
    canonical, member = f"{owner}::node::00000000", f"{owner}::node::00000001"
    backend.insert_node(owner, _node(canonical, "Canonical", 2), _vec(0))  # type: ignore[arg-type]
    backend.insert_node(owner, _node(member, "Member", 1), _vec(0, 0.85))  # type: ignore[arg-type,call-arg]
    backend.upsert_edge(
        owner,
        TypedLink(
            id=make_edge_id(canonical, member, LinkType.SEMANTIC),
            src_node_id=canonical,
            dst_node_id=member,
            link_type=LinkType.SEMANTIC,
            weight=0.85,
            created_at=t0,
            valid_at=t0,
        ),
    )

    registry = _registry(rls_engine, embedder, str(tmp_path))
    worker = Worker(
        dispatch_engine=rls_engine, rls_engine=rls_engine, registry=registry, worker_id="pm-cons"
    )
    enqueue_graph_consolidation(
        JobQueue(rls_engine), owner_id=owner, delay_seconds=0.0, bucket_seconds=1.0
    )
    assert _drain(worker) >= 1  # claim → ConsolidationHandler → pass.run

    with seed_engine.begin() as conn:
        merged_into = conn.execute(
            text("SELECT merged_into FROM graph_nodes WHERE owner_id = :o AND id = :i"),
            {"o": owner, "i": member},
        ).scalar_one_or_none()
    assert merged_into is not None  # K7 consolidation MERGED the band-dup (real output)


def test_managed_k8_gist_engine_runs_and_produces_gist(
    rls_engine: Engine,
    seed_engine: Engine,
    embedder: HashEmbedder384,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """K8 sleep-time engine: the REAL turn-end producer → worker fires → gist + episode."""
    monkeypatch.setenv("PERSONA_EPISODIC_ENGINE_ENABLED", "true")
    monkeypatch.setenv("PERSONA_EPISODIC_IDLE_DELAY_SECONDS", "0")
    monkeypatch.setenv("PERSONA_EPISODIC_BUCKET_SECONDS", "1")
    monkeypatch.setenv("PERSONA_EPISODIC_MIN_CHUNKS_PER_RUN", "2")
    from persona_api.services.synthesis_trigger import enqueue_conversation_synthesis

    owner, persona = "pm_k8_owner", "pm_k8_persona"
    with seed_engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email) VALUES (:o, 'k@ex.com')"), {"o": owner})
        conn.execute(
            text("INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :o, 'name: k')"),
            {"p": persona, "o": owner},
        )
    backend = PostgresBackend(engine=seed_engine, embedder=embedder)
    raw_ids: list[str] = []
    for i, line in enumerate(
        ("USER: we moved to Oslo\nASSISTANT: great", "USER: job starts Monday\nASSISTANT: nice")
    ):
        cid = mint_chunk_id(persona, "episodic")
        created = datetime.now(UTC) - timedelta(hours=3) + timedelta(minutes=i)
        raw_ids.append(cid)
        backend.upsert(
            persona_id=persona,
            store_kind="episodic",
            chunks=[
                PersonaChunk(
                    id=cid,
                    text=line,
                    created_at=created,
                    provenance=ChunkProvenance(
                        source=WriteSource.SYSTEM,
                        logical_id=cid,
                        version=1,
                        written_at=created,
                        written_by="runtime.loop",
                    ),
                )
            ],
        )

    registry = _registry(rls_engine, embedder, str(tmp_path))
    assert EPISODIC_CONSOLIDATION_JOB_TYPE in registry.types()  # type: ignore[attr-defined]
    worker = Worker(
        dispatch_engine=rls_engine, rls_engine=rls_engine, registry=registry, worker_id="pm-k8"
    )
    # THE REAL BOUNDARY — the exact producer a completed turn calls (no hand-enqueue).
    enqueue_conversation_synthesis(
        JobQueue(rls_engine),
        owner_id=owner,
        conversation_id="pm-k8-convo",
        persona_id=persona,
        message_count=2,
    )
    assert _drain(worker) >= 1  # the worker fired the coalesced episodic job

    with seed_engine.begin() as conn:
        gists = conn.execute(
            text(
                "SELECT member_ids FROM memory_chunks "
                "WHERE persona_id = :p AND kind = 'episodic_gist'"
            ),
            {"p": persona},
        ).all()
        # The episode concept node lands via the engine's real K7 graph.merge. Its
        # name is derived from the gist's opening words (not a fixed prefix), so count
        # the owner's graph nodes: the graph was empty and ONLY the episodic engine
        # writes for this owner, so any node present is the episode it produced.
        episode_nodes = conn.execute(
            text("SELECT count(*) FROM graph_nodes WHERE owner_id = :o"),
            {"o": owner},
        ).scalar_one()
        raw_after = conn.execute(
            text("SELECT count(*) FROM memory_chunks WHERE persona_id = :p AND kind = 'episodic'"),
            {"p": persona},
        ).scalar_one()
    assert len(gists) == 1  # K8 produced a real gist through the fire
    assert set(gists[0].member_ids) == set(raw_ids)
    assert episode_nodes >= 1  # and its episode concept node landed via the real merge
    assert raw_after == len(raw_ids)  # raw layer untouched (pyramid, not destructive)


# ---- community-legacy-sqlite column: HONEST dark cells (D-5 deprecation cost) ----


def test_legacy_sqlite_has_no_graph_tables() -> None:
    """The graph is DARK on legacy SQLite — graph_* are excluded from the schema."""
    from persona_api.db.community import build_community_metadata

    tables = set(build_community_metadata().tables)
    for graph_table in (
        "graph_nodes",
        "graph_edges",
        "graph_entities",
        "graph_node_versions",
        "graph_consolidation_markers",
        "synthesis_markers",
    ):
        assert graph_table not in tables  # no graph ⇒ no synthesis/K7/self-node on legacy
    # typed-memory app tables ARE present (the data model works via Chroma).
    assert "conversations" in tables
    assert "messages" in tables
    assert "personas" in tables


def test_legacy_sqlite_worker_is_refused() -> None:
    """The worker (K2/K7/K8 driver) REFUSES on legacy SQLite → those passes are dark."""
    from persona_api.background.worker_root import start_in_process_worker
    from persona_api.errors import CommunityDbError

    engine = create_engine("sqlite+pysqlite:///:memory:")
    with pytest.raises(CommunityDbError) as exc:
        start_in_process_worker(
            config=APIConfig(),
            rls_engine=engine,
            embedder=object(),  # type: ignore[arg-type]
            tier_registry=object(),  # type: ignore[arg-type]
        )
    assert exc.value.context["reason"] == "worker_requires_postgres"
