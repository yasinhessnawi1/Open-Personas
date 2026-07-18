"""Cross-layer forget — the Balto reproduction (Spec K11, T1).

Found in K8's operator pass (persona Office Iris): the owner deleted the "Balto the
dog" concept-graph fact via ``/memory``, then in a new chat the persona still recalled
Balto. Deletion did not span the two memory layers, and the layer that actually
drives recall (episodic) had no user-facing delete at all — so the next sleep-time
consolidation pass re-distilled the deleted fact right back (resurrection), since the
raw evidence it was distilled from was still alive.

This is the end-to-end reproduction + fix, driven through the REAL stack — no
hand-forced state (the A4 lesson: forcing "forgotten" by hand would prove nothing
about the actual trigger chain):

- A REAL sleep-time consolidation pass (the actual ``EpisodicConsolidationEngine``:
  ``StubSummarizer`` + the K7 graph merge) distills a raw episodic chunk into a gist
  AND a concept-graph node.
- REAL recall (``EpisodicStore.query``, the exact method the chat/voice loop calls)
  surfaces it before the forget.
- The REAL ``/v1/memory/nodes/{id}/forget-preview`` + ``/forget`` HTTP routes (Spec
  K11) do the cross-layer delete.
- Recall no longer surfaces it, AND a SECOND real consolidation pass does not
  re-distill it — the resurrection guard (D-K11-2): starving the episodic evidence,
  not a tombstone, is what prevents it.

A negative-control chunk (unrelated content, seeded far enough apart in time to form
its own consolidation window) proves the D-K11-1 similarity floor actually
discriminates rather than surfacing everything as a candidate.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from persona.schema.chunks import ChunkProvenance, PersonaChunk, WriteSource, mint_chunk_id
from persona.stores.engine import EpisodicConsolidationEngine
from persona.stores.episodic import EpisodicStore
from persona.stores.lifecycle import EpisodicSettings
from persona.stores.pyramid import EpisodicPyramid
from persona.stores.summarizer import StubSummarizer
from persona_api.app import create_app
from persona_api.auth import AuthenticatedUser
from persona_api.config import APIConfig
from persona_api.middleware.rls_context import current_user_id
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy import Engine
    from tests.conftest import HashEmbedder384

pytestmark = pytest.mark.integration

_OWNER = "u_forget_balto"
_PERSONA = "p_forget_balto"
# A second persona for the SAME owner (K11-T1 follow-up): the concept graph is
# user-wide but episodic is per-persona (D-K11-3), so a forget must scan every one
# of the owner's personas, not just the one a node happened to be distilled from.
_PERSONA_B = "p_forget_balto_b"
_NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
# min_chunks_per_run=1: a single seeded exchange is enough to trigger a pass — the
# production default (5) exists to avoid gisting a half-formed session, orthogonal to
# what this test proves. cluster_gap_minutes stays the (generous) default so the two
# seeded chunks below land in separate windows purely from their time gap.
_SETTINGS = EpisodicSettings(min_chunks_per_run=1, cluster_gap_minutes=45.0)

# No sentence-ending punctuation ("." "!" "?") so the engine's episode-label
# derivation (persona.stores.engine._episode_label) keeps "Balto" in the label, and
# the whole text survives the summarizer's (no-op, under-budget) truncation intact —
# so the concept node's content is BYTE-IDENTICAL to the raw chunk it was distilled
# from, making the forget-preview's self-similarity assertion deterministic under a
# hash embedder (no real-model paraphrase quality is under test here — D-K11-1's
# floor MECHANISM is; see the negative control for the discrimination proof).
_BALTO_TEXT = (
    "USER: I have a dog named Balto\nASSISTANT: That is wonderful, tell me more about Balto"
)
_WEATHER_TEXT = (
    "USER: what is the weather like today\nASSISTANT: sunny and warm, a great day outside"
)


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {_OWNER}"}


@pytest.fixture
def app_client(
    migrated_engine: Engine, embedder: HashEmbedder384, tmp_path: Path
) -> Iterator[TestClient]:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set; skipping K11 cross-layer forget test")
    with migrated_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:o, 'forget-balto@example.com')"),
            {"o": _OWNER},
        )
        conn.execute(
            text(
                "INSERT INTO personas (id, owner_id, yaml) "
                "VALUES (:p, :o, 'schema_version: \"1.0\"')"
            ),
            {"p": _PERSONA, "o": _OWNER},
        )

    cfg = APIConfig(app_database_url=app_url, audit_root=str(tmp_path / "audit"))
    app = create_app(cfg)

    async def _fake_verify(token: str) -> AuthenticatedUser:
        return AuthenticatedUser(id=token, email=None)  # token == user_id, the test convention

    with TestClient(app) as client:
        app.state.verify_token = _fake_verify
        assert app.state.graph_store is not None, "graph_store must compose on Postgres"
        assert app.state.memory_backend is not None, "memory_backend must compose on Postgres"
        # A deterministic, fast embedder (mirrors test_memory_routes_rls_postgres.py's
        # seam-swap): the assertions below hinge on exact-string self-similarity, not
        # paraphrase quality, so real_embedder's slow model load buys nothing here.
        app.state.graph_store._embedder = embedder  # noqa: SLF001 — test seam
        app.state.memory_backend._embedder = embedder  # noqa: SLF001 — test seam
        yield client
    with migrated_engine.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id = :o"), {"o": _OWNER})


def _chunk(*, hours_ago: float, body: str, persona_id: str = _PERSONA) -> PersonaChunk:
    cid = mint_chunk_id(persona_id, "episodic")
    created = _NOW - timedelta(hours=hours_ago)
    return PersonaChunk(
        id=cid,
        text=body,
        created_at=created,
        provenance=ChunkProvenance(
            source=WriteSource.USER,
            logical_id=cid,
            version=1,
            written_at=created,
            written_by=_OWNER,
        ),
    )


def _write_episodic(
    client: TestClient, *, hours_ago: float, body: str, persona_id: str = _PERSONA
) -> None:
    """A real episodic write on the real RLS-scoped backend (not an admin bypass)."""
    token = current_user_id.set(_OWNER)
    try:
        chunk = _chunk(hours_ago=hours_ago, body=body, persona_id=persona_id)
        client.app.state.memory_backend.upsert(
            persona_id=persona_id, store_kind="episodic", chunks=[chunk]
        )
    finally:
        current_user_id.reset(token)


def _run_consolidation_pass(client: TestClient) -> None:
    """One REAL sleep-time consolidation pass — the exact engine the A0 worker runs."""
    backend = client.app.state.memory_backend
    audit = client.app.state.audit_logger
    engine = EpisodicConsolidationEngine(
        backend=backend,
        pyramid=EpisodicPyramid(backend=backend, audit_logger=audit),
        summarizer=StubSummarizer(),
        graph=client.app.state.graph_store,
        settings=_SETTINGS,
    )
    token = current_user_id.set(_OWNER)
    try:
        asyncio.run(engine.run(_OWNER, _PERSONA, now=_NOW))
    finally:
        current_user_id.reset(token)


def _recall_returns(
    client: TestClient, query: str, *, contains: str, persona_id: str = _PERSONA
) -> bool:
    """REAL recall — the exact ``EpisodicStore.query`` the chat/voice loop calls."""
    store = EpisodicStore(
        backend=client.app.state.memory_backend, audit_logger=client.app.state.audit_logger
    )
    token = current_user_id.set(_OWNER)
    try:
        hits = store.query(persona_id, query, 5)
    finally:
        current_user_id.reset(token)
    return any(contains in h.text for h in hits)


def _find_graph_node(client: TestClient, *, contains: str) -> dict[str, object] | None:
    """Through the REAL ``GET /v1/memory/graph`` route (the seed window)."""
    resp = client.get("/v1/memory/graph", headers=_auth())
    assert resp.status_code == 200
    for node in resp.json()["nodes"]:
        if contains.lower() in str(node["label"]).lower():
            return node
    return None


def test_forget_deletes_episodic_and_survives_reconsolidation(app_client: TestClient) -> None:
    """The Balto reproduction (spec K11 §6, the gate): forget survives a real re-run."""
    _write_episodic(app_client, hours_ago=10.0, body=_WEATHER_TEXT)  # the negative control
    _write_episodic(app_client, hours_ago=2.0, body=_BALTO_TEXT)
    _run_consolidation_pass(app_client)

    node = _find_graph_node(app_client, contains="Balto")
    assert node is not None, "consolidation should have distilled a Balto concept node"
    assert _recall_returns(app_client, "dog", contains="Balto")

    # Act: preview then forget the confirmed evidence.
    prev = app_client.post(f"/v1/memory/nodes/{node['id']}/forget-preview", headers=_auth())
    assert prev.status_code == 200
    candidates = prev.json()["candidates"]
    assert candidates, "the raw Balto chunk should clear the similarity floor"
    assert all("Balto" in c["text"] for c in candidates), candidates
    assert all(c["persona_id"] == _PERSONA and c["kind"] == "raw" for c in candidates), candidates
    # The negative control: the unrelated weather chunk must NOT clear the floor —
    # proves the D-K11-1 floor discriminates rather than surfacing everything.
    assert not any("weather" in c["text"].lower() for c in candidates), candidates

    commit = app_client.post(
        f"/v1/memory/nodes/{node['id']}/forget",
        headers=_auth(),
        json={
            "episodic": [
                {"persona_id": c["persona_id"], "chunk_id": c["chunk_id"]} for c in candidates
            ]
        },
    )
    assert commit.status_code == 204

    # Assert: gone from recall now...
    assert not _recall_returns(app_client, "dog", contains="Balto")
    assert _find_graph_node(app_client, contains="Balto") is None

    # ...AND after a second consolidation pass (no resurrection, D-K11-2): the raw
    # evidence the engine would need to re-distill it from is gone, not tombstoned.
    _run_consolidation_pass(app_client)
    assert not _recall_returns(app_client, "dog", contains="Balto")
    assert _find_graph_node(app_client, contains="Balto") is None

    # The unrelated memory is untouched throughout — forget is scoped, not global.
    assert _recall_returns(app_client, "weather", contains="sunny")


def test_forget_scans_every_owned_persona(app_client: TestClient, migrated_engine: Engine) -> None:
    """K11-T1 follow-up (Important): the cross-persona scan the single-persona
    reproduction above never exercised.

    Two personas for the SAME owner; only persona A's raw chunk was ever
    consolidated into the Balto concept node, but persona B independently holds its
    own raw chunk that semantically matches that node's content too. D-K11-3: the
    concept graph is user-wide, episodic is per-persona — so forget-preview/forget
    must reach BOTH personas' episodic stores for one node, not just the persona
    that happened to produce it.
    """
    with migrated_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO personas (id, owner_id, yaml) "
                "VALUES (:p, :o, 'schema_version: \"1.0\"')"
            ),
            {"p": _PERSONA_B, "o": _OWNER},
        )

    _write_episodic(app_client, hours_ago=2.0, body=_BALTO_TEXT)
    _run_consolidation_pass(app_client)
    node = _find_graph_node(app_client, contains="Balto")
    assert node is not None, "consolidation should have distilled a Balto concept node"

    # Persona B never runs consolidation — its raw chunk stays raw, but still clears
    # the similarity floor against the SAME node content.
    _write_episodic(app_client, hours_ago=1.0, body=_BALTO_TEXT, persona_id=_PERSONA_B)

    prev = app_client.post(f"/v1/memory/nodes/{node['id']}/forget-preview", headers=_auth())
    assert prev.status_code == 200
    candidates = prev.json()["candidates"]
    persona_ids = {c["persona_id"] for c in candidates}
    assert persona_ids == {_PERSONA, _PERSONA_B}, candidates
    assert all(c["kind"] == "raw" for c in candidates), candidates

    commit = app_client.post(
        f"/v1/memory/nodes/{node['id']}/forget",
        headers=_auth(),
        json={
            "episodic": [
                {"persona_id": c["persona_id"], "chunk_id": c["chunk_id"]} for c in candidates
            ]
        },
    )
    assert commit.status_code == 204

    # Gone from recall in BOTH personas, and the (user-wide) node is gone too.
    assert not _recall_returns(app_client, "dog", contains="Balto")
    assert not _recall_returns(app_client, "dog", contains="Balto", persona_id=_PERSONA_B)
    assert _find_graph_node(app_client, contains="Balto") is None


def test_forget_routes_404_for_a_missing_node(app_client: TestClient) -> None:
    """Existence-disclosure-safe 404 (mirrors the K5 ``delete_node`` pattern)."""
    missing = "does-not-exist"
    prev = app_client.post(f"/v1/memory/nodes/{missing}/forget-preview", headers=_auth())
    assert prev.status_code == 404

    commit = app_client.post(
        f"/v1/memory/nodes/{missing}/forget", headers=_auth(), json={"episodic": []}
    )
    assert commit.status_code == 404
