"""The standalone episodic browser API — list, drill, delete with cascade (Spec K11, T2).

Task 1 built the cross-layer forget (concept-node delete → episodic evidence).
This is the browser's OWN surface (D-K11-5): see and delete a persona's episodic
memory directly, rendered as its own gist-layer graph, separate from the concept
graph. The routes ride the same real Postgres + RLS stack Task 1's
``test_forget_crosslayer.py`` proved — ``memory_backend`` composed on the
per-request RLS engine (D-08-1), so ownership is enforced in-kernel, never by an
application-level check.

Gists are seeded directly via ``EpisodicPyramid.write_gist`` (bypassing the real
sleep-time consolidation engine) — the engine's own clustering/distillation logic
is proven elsewhere (``test_episodic_engine_wired.py``, ``test_forget_crosslayer.py``);
this file exercises the browser's read/drill/delete surface over an
already-consolidated window, the exact row shape the engine produces.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from persona.schema.chunks import ChunkProvenance, PersonaChunk, WriteSource, mint_chunk_id
from persona.stores.pyramid import EpisodicPyramid
from persona_api.app import create_app
from persona_api.auth import AuthenticatedUser
from persona_api.config import APIConfig
from persona_api.middleware.rate_limit import InMemoryRateLimitStore, RateLimiter
from persona_api.middleware.rls_context import current_user_id
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy import Engine
    from tests.conftest import HashEmbedder384

pytestmark = pytest.mark.integration

_OWNER = "u_episodic_browser"
_OWNER_B = "u_episodic_browser_b"
_PERSONA = "p_episodic_browser"
_NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


def _auth(user: str = _OWNER) -> dict[str, str]:
    return {"Authorization": f"Bearer {user}"}


@pytest.fixture
def app_client(
    migrated_engine: Engine, embedder: HashEmbedder384, tmp_path: Path
) -> Iterator[TestClient]:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set; skipping K11 episodic browser test")
    with migrated_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:o, 'episodic-browser@example.com')"),
            {"o": _OWNER},
        )
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:o, 'episodic-browser-b@example.com')"),
            {"o": _OWNER_B},
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
        assert app.state.memory_backend is not None, "memory_backend must compose on Postgres"
        # A deterministic, fast embedder (mirrors test_forget_crosslayer.py's seam-swap):
        # the assertions hinge on exact-string self-similarity, not paraphrase quality.
        app.state.memory_backend._embedder = embedder  # noqa: SLF001 — test seam
        yield client
    with migrated_engine.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id IN (:a, :b)"), {"a": _OWNER, "b": _OWNER_B})


def _chunk(*, minutes_ago: float, body: str, persona_id: str = _PERSONA) -> PersonaChunk:
    cid = mint_chunk_id(persona_id, "episodic")
    created = _NOW - timedelta(minutes=minutes_ago)
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


def _seed_episodic_with_gist(
    client: TestClient,
    *,
    members: list[str],
    persona_id: str = _PERSONA,
    gist_text: str | None = None,
) -> tuple[str, list[str]]:
    """Write raw episodic chunks AND their covering gist directly (real RLS-scoped
    writes, not an admin bypass) — returns ``(gist_id, member_chunk_ids)``."""
    token = current_user_id.set(_OWNER)
    try:
        chunks = [
            _chunk(minutes_ago=len(members) - i, body=body, persona_id=persona_id)
            for i, body in enumerate(members)
        ]
        client.app.state.memory_backend.upsert(
            persona_id=persona_id, store_kind="episodic", chunks=chunks
        )
        pyramid = EpisodicPyramid(
            backend=client.app.state.memory_backend, audit_logger=client.app.state.audit_logger
        )
        gist = pyramid.write_gist(
            persona_id,
            text=gist_text or " / ".join(members),
            member_ids=[c.id for c in chunks],
            created_at=_NOW,
        )
    finally:
        current_user_id.reset(token)
    return gist.id, [c.id for c in chunks]


def _raw_chunk_count(client: TestClient, persona_id: str = _PERSONA) -> int:
    token = current_user_id.set(_OWNER)
    try:
        chunks = client.app.state.memory_backend.get_all(
            persona_id=persona_id, store_kind="episodic"
        )
    finally:
        current_user_id.reset(token)
    return len(chunks)


def test_episodic_window_lists_gists_and_drills(app_client: TestClient) -> None:
    gid, member_ids = _seed_episodic_with_gist(app_client, members=["USER: a", "USER: b"])

    win = app_client.get(
        "/v1/memory/episodic", params={"persona_id": _PERSONA}, headers=_auth()
    ).json()
    assert win["available"]
    assert len(win["gists"]) == 1
    assert win["gists"][0]["id"] == gid
    assert set(win["gists"][0]["member_ids"]) == set(member_ids)

    mem = app_client.get(
        f"/v1/memory/episodic/{gid}/members", params={"persona_id": _PERSONA}, headers=_auth()
    ).json()
    assert len(mem["members"]) == 2
    assert {m["id"] for m in mem["members"]} == set(member_ids)


def test_episodic_window_search_returns_the_covering_gist(app_client: TestClient) -> None:
    """``q`` runs the real ``episodic.query`` recall and reports the hit's gist (D-K11-5)."""
    gid, _ = _seed_episodic_with_gist(
        app_client,
        members=["USER: I have a dog named Balto"],
        gist_text="Balto the dog",
    )
    # A second, unrelated gist proves the search actually discriminates.
    _seed_episodic_with_gist(
        app_client,
        members=["USER: the weather is sunny today"],
        gist_text="sunny weather",
    )

    # Verbatim self-similarity (mirrors test_forget_crosslayer.py's rationale): the
    # HashEmbedder384 has zero semantic structure, so an exact-text query is what
    # makes this deterministic — the real recall model's paraphrase quality is not
    # under test here.
    win = app_client.get(
        "/v1/memory/episodic",
        params={"persona_id": _PERSONA, "q": "USER: I have a dog named Balto"},
        headers=_auth(),
    ).json()
    assert win["available"]
    assert any(g["id"] == gid for g in win["gists"])


def test_delete_gist_removes_members_and_cascades(app_client: TestClient) -> None:
    gid, _member_ids = _seed_episodic_with_gist(app_client, members=["USER: a", "USER: b"])

    resp = app_client.delete(
        f"/v1/memory/episodic/{gid}",
        params={"persona_id": _PERSONA, "is_gist": "true"},
        headers=_auth(),
    )
    assert resp.status_code == 204

    win = app_client.get(
        "/v1/memory/episodic", params={"persona_id": _PERSONA}, headers=_auth()
    ).json()
    assert win["gists"] == []
    assert _raw_chunk_count(app_client) == 0  # members gone


def test_delete_raw_chunk_cascades_its_covering_gist(app_client: TestClient) -> None:
    """D-K11-6: a raw-chunk delete cascades its gist (K8-D-14); the sibling member survives."""
    gid, member_ids = _seed_episodic_with_gist(app_client, members=["USER: a", "USER: b"])

    resp = app_client.delete(
        f"/v1/memory/episodic/{member_ids[0]}",
        params={"persona_id": _PERSONA, "is_gist": "false"},
        headers=_auth(),
    )
    assert resp.status_code == 204

    win = app_client.get(
        "/v1/memory/episodic", params={"persona_id": _PERSONA}, headers=_auth()
    ).json()
    assert win["gists"] == []  # the covering gist was cascaded away
    assert _raw_chunk_count(app_client) == 1  # the sibling raw member survives


def test_delete_missing_episodic_node_is_404(app_client: TestClient) -> None:
    resp = app_client.delete(
        "/v1/memory/episodic/does-not-exist",
        params={"persona_id": _PERSONA, "is_gist": "false"},
        headers=_auth(),
    )
    assert resp.status_code == 404


def test_episodic_window_reports_unavailable_when_no_backend() -> None:
    """``available=False`` mirrors the K5 graph route when no episodic backend is composed."""
    app = create_app(APIConfig())

    async def _verify(token: str) -> AuthenticatedUser:
        return AuthenticatedUser(id=token, email=None)

    app.state.verify_token = _verify
    app.state.rls_engine = None
    app.state.memory_backend = None
    app.state.rate_limiter = RateLimiter(
        InMemoryRateLimitStore(), default_limit=1000, per_endpoint={}
    )
    resp = TestClient(app).get("/v1/memory/episodic", params={"persona_id": "p"}, headers=_auth())
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert body["gists"] == []


# ---- R-K11-T2-RLS: a second owner's GET/DELETE never reach this persona's episodic ----


def test_cross_owner_get_and_delete_touch_nothing(app_client: TestClient) -> None:
    gid, member_ids = _seed_episodic_with_gist(app_client, members=["USER: a", "USER: b"])

    # Owner B's GET reads through the REAL per-request RLS engine: `_PERSONA` is not
    # theirs, so the `memory_chunks` owner predicate denies in-kernel — empty, never
    # a leak (the K5-proven pattern; a mis-bound/superuser engine would 200 non-empty).
    cross_get = app_client.get(
        "/v1/memory/episodic", params={"persona_id": _PERSONA}, headers=_auth(_OWNER_B)
    )
    assert cross_get.status_code == 200
    assert cross_get.json()["gists"] == []

    cross_members = app_client.get(
        f"/v1/memory/episodic/{gid}/members",
        params={"persona_id": _PERSONA},
        headers=_auth(_OWNER_B),
    )
    assert cross_members.status_code == 200
    assert cross_members.json()["members"] == []

    # Owner B's DELETE (gist AND raw) 404s — the underlying gists()/get_by_logical_ids
    # lookups are RLS-scoped to nothing, so episodic_delete reports "not found" rather
    # than reaching owner A's rows.
    cross_delete_gist = app_client.delete(
        f"/v1/memory/episodic/{gid}",
        params={"persona_id": _PERSONA, "is_gist": "true"},
        headers=_auth(_OWNER_B),
    )
    assert cross_delete_gist.status_code == 404

    cross_delete_raw = app_client.delete(
        f"/v1/memory/episodic/{member_ids[0]}",
        params={"persona_id": _PERSONA, "is_gist": "false"},
        headers=_auth(_OWNER_B),
    )
    assert cross_delete_raw.status_code == 404

    # Owner A's data is completely untouched.
    still_there = app_client.get(
        "/v1/memory/episodic", params={"persona_id": _PERSONA}, headers=_auth(_OWNER)
    )
    assert len(still_there.json()["gists"]) == 1
    assert _raw_chunk_count(app_client) == len(member_ids)
