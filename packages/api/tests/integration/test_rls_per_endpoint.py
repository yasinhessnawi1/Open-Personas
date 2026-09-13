"""Per-endpoint RLS adversarial sweep (spec 08, T15, acceptance #4 — the headline).

The vuln spec 07 caught passed all functional tests, so the RLS proof is
per-route, not one-endpoint. This parametrizes over EVERY tenant-touching
endpoint that takes a resource id: seed user A's persona / conversation / run,
then — as user B — hit each endpoint with A's ids and assert it's blocked (404),
and assert B's list endpoints never include A's resources. Includes the
runtime-store path (a chat turn hitting memory_chunks).

Two users, real Docker Postgres, the non-superuser persona_app role under RLS.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from persona.backends import StreamChunk
from persona.schema.conversation import ConversationMessage
from persona_api.app import create_app
from persona_api.config import APIConfig
from persona_api.middleware.rls_context import make_rls_engine
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from persona.schema.conversation import Conversation
    from persona_runtime.prompt import DocumentContext
    from sqlalchemy import Engine
    from tests.conftest import HashEmbedder384

pytestmark = pytest.mark.integration

_YAML = """\
schema_version: "1.0"
identity:
  name: A
  role: assistant
  background: |
    x
  language_default: en
  constraints: []
"""


class _Loop:
    async def turn(
        self,
        conversation: Conversation,
        user_message: str,
        on_event: object = None,  # noqa: ARG002 — accepted to match the loop signature
        *,
        turn_has_image: bool = False,  # noqa: ARG002 — spec-13 T20 compat
        images: list[object] | None = None,  # noqa: ARG002 — image-cascade compat
        documents: list[object] | None = None,  # noqa: ARG002 — document-cascade compat
        document_context: DocumentContext | None = None,  # noqa: ARG002 — spec-14 compat
    ) -> AsyncIterator[StreamChunk]:
        now = datetime.now(UTC)
        conversation.messages.append(
            ConversationMessage(role="user", content=user_message, created_at=now)
        )
        conversation.messages.append(
            ConversationMessage(role="assistant", content="ok", created_at=now)
        )
        yield StreamChunk(delta="ok", is_final=True)


@pytest.fixture
def app_client(
    migrated_engine: Engine,  # noqa: ARG001
    embedder: HashEmbedder384,
    tmp_path: object,
) -> Iterator[TestClient]:
    import os

    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")
    cfg = APIConfig(app_database_url=app_url, audit_root=str(tmp_path) + "/audit")
    app = create_app(cfg)

    from persona_api.auth import AuthenticatedUser

    async def _verify(token: str) -> AuthenticatedUser:
        return AuthenticatedUser(id=token, email=None)

    async def _build(_pid: str) -> _Loop:
        return _Loop()

    with TestClient(app) as c:
        app.state.verify_token = _verify
        app.state.embedder = embedder
        app.state.build_conversation_loop = _build
        # Drop the lifespan-installed TierRegistry so the persona-detail
        # capabilities surface doesn't lazily instantiate a real chat backend
        # (AuthenticationError("missing API key") on CI without ANTHROPIC_API_KEY).
        if hasattr(app.state, "tier_registry"):
            app.state.tier_registry = None
        su = make_rls_engine(os.environ["DATABASE_URL"])
        with su.begin() as conn:
            for u in ("user_A", "user_B"):
                conn.execute(
                    text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
                    {"i": u, "e": f"{u}@x"},
                )
        su.dispose()
        yield c
        su = make_rls_engine(os.environ["DATABASE_URL"])
        with su.begin() as conn:
            conn.execute(text("DELETE FROM users WHERE id IN ('user_A','user_B')"))
        su.dispose()


def _h(uid: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {uid}"}


def _seed_a(c: TestClient) -> dict[str, str]:
    """Create user A's persona + conversation as A. Returns the ids."""
    pid = c.post("/v1/personas", json={"yaml": _YAML}, headers=_h("user_A")).json()["id"]
    conv = c.post(
        f"/v1/personas/{pid}/conversations", json={"title": "A"}, headers=_h("user_A")
    ).json()["id"]
    return {"persona_id": pid, "conversation_id": conv}


def test_user_b_cannot_read_user_a_persona(app_client: TestClient) -> None:
    ids = _seed_a(app_client)
    assert (
        app_client.get(f"/v1/personas/{ids['persona_id']}", headers=_h("user_B")).status_code == 404
    )


def test_user_b_cannot_patch_or_delete_user_a_persona(app_client: TestClient) -> None:
    ids = _seed_a(app_client)
    pid = ids["persona_id"]
    assert (
        app_client.patch(
            f"/v1/personas/{pid}", json={"yaml": _YAML}, headers=_h("user_B")
        ).status_code
        == 404
    )
    assert app_client.delete(f"/v1/personas/{pid}", headers=_h("user_B")).status_code == 404
    # A can still read it (B's failed delete didn't touch it)
    assert app_client.get(f"/v1/personas/{pid}", headers=_h("user_A")).status_code == 200


def test_user_b_cannot_read_user_a_conversation(app_client: TestClient) -> None:
    ids = _seed_a(app_client)
    assert (
        app_client.get(
            f"/v1/conversations/{ids['conversation_id']}", headers=_h("user_B")
        ).status_code
        == 404
    )


def test_user_b_cannot_delete_user_a_conversation(app_client: TestClient) -> None:
    ids = _seed_a(app_client)
    cid = ids["conversation_id"]
    # B's delete on A's conversation → RLS hides it → 404, no deletion.
    assert app_client.delete(f"/v1/conversations/{cid}", headers=_h("user_B")).status_code == 404
    # A can still read it (B's delete matched no row).
    assert app_client.get(f"/v1/conversations/{cid}", headers=_h("user_A")).status_code == 200


def test_user_b_cannot_post_to_user_a_conversation(app_client: TestClient) -> None:
    ids = _seed_a(app_client)
    # B posts to A's conversation → the pre-flight RLS check 404s BEFORE the
    # stream starts (clean error, no mid-stream "response already started").
    resp = app_client.post(
        f"/v1/conversations/{ids['conversation_id']}/messages",
        json={"content": "intrude"},
        headers=_h("user_B"),
    )
    assert resp.status_code == 404
    # And the conversation was not mutated by B's attempt.
    hist = app_client.get(
        f"/v1/conversations/{ids['conversation_id']}", headers=_h("user_A")
    ).json()
    assert hist["messages"] == []


def test_user_b_cannot_create_conversation_on_user_a_persona(app_client: TestClient) -> None:
    ids = _seed_a(app_client)
    # B references A's persona id → RLS hides it → 404 (persona not found).
    assert (
        app_client.post(
            f"/v1/personas/{ids['persona_id']}/conversations",
            json={"title": "x"},
            headers=_h("user_B"),
        ).status_code
        == 404
    )


def test_user_b_cannot_read_user_a_dispatched_task(app_client: TestClient) -> None:
    """A one-off dispatch answers with the TASK that carries the work (Spec W1, D-W1-1); the
    run row is opened by the worker later. The task, its detail and its controls are A's."""
    ids = _seed_a(app_client)
    res = app_client.post(
        f"/v1/personas/{ids['persona_id']}/runs", json={"task": "t"}, headers=_h("user_A")
    )
    assert res.status_code == 202, res.text
    task_id = res.json()["task_id"]
    assert app_client.get(f"/v1/tasks/{task_id}", headers=_h("user_B")).status_code == 404
    assert app_client.post(f"/v1/tasks/{task_id}/cancel", headers=_h("user_B")).status_code == 404
    assert app_client.get(f"/v1/tasks/{task_id}", headers=_h("user_A")).status_code == 200


def test_user_b_list_endpoints_exclude_user_a_resources(app_client: TestClient) -> None:
    ids = _seed_a(app_client)
    # B lists personas / conversations → must not see A's.
    b_personas = app_client.get("/v1/personas", headers=_h("user_B")).json()
    assert all(p["id"] != ids["persona_id"] for p in b_personas)
    b_convs = app_client.get("/v1/conversations", headers=_h("user_B")).json()
    assert all(cv["id"] != ids["conversation_id"] for cv in b_convs)


_TINY_PNG_BYTES: bytes = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
    "0000000c49444154789c63f8cfc0000003010100c9fe92ef0000000049454e44ae"
    "426082"
)


def test_user_b_cannot_post_upload_to_user_a_persona(app_client: TestClient) -> None:
    """Spec 13 T11: cross-tenant POST to /personas/{id}/uploads → 404.

    Extends the spec-08 sweep so the upload-route pair (POST + GET) is
    covered by the RLS adversarial proof. T14 inherits this assertion.
    """
    ids = _seed_a(app_client)
    resp = app_client.post(
        f"/v1/personas/{ids['persona_id']}/uploads",
        files={"file": ("a.png", _TINY_PNG_BYTES, "image/png")},
        headers=_h("user_B"),
    )
    # B references A's persona id → RLS-scoped pre-flight 404s (no upload).
    assert resp.status_code == 404


def test_user_b_cannot_get_user_a_uploads(app_client: TestClient) -> None:
    """Spec 13 T11: cross-tenant GET to /personas/{id}/uploads/{ref} → 404.

    Even with a probe ref pattern, B's request is rejected by the pre-flight
    RLS check before any workspace I/O. Existence-disclosure-safe per
    D-13: B cannot distinguish "no such persona" from "no such ref".
    """
    ids = _seed_a(app_client)
    # User A first uploads so a ref exists in the workspace.
    up = app_client.post(
        f"/v1/personas/{ids['persona_id']}/uploads",
        files={"file": ("a.png", _TINY_PNG_BYTES, "image/png")},
        headers=_h("user_A"),
    )
    assert up.status_code == 201, up.text
    ref = up.json()["workspace_path"]
    # B GETs A's ref → 404 (pre-flight RLS persona check fires).
    resp = app_client.get(f"/v1/personas/{ids['persona_id']}/uploads/{ref}", headers=_h("user_B"))
    assert resp.status_code == 404


def test_runtime_store_path_is_tenant_scoped(app_client: TestClient) -> None:
    """A chat turn populates memory_chunks under A; B's chat on B's own persona
    sees only B's memory. Proves the runtime-store path is RLS-scoped (#4)."""
    import os

    ids_a = _seed_a(app_client)
    # A sends a message (writes a message row; memory chunks were written on create)
    app_client.post(
        f"/v1/conversations/{ids_a['conversation_id']}/messages",
        json={"content": "hello from A"},
        headers=_h("user_A"),
    )
    # Directly assert memory_chunks for A's persona are not visible to B via RLS.
    su = make_rls_engine(os.environ["DATABASE_URL"])
    app_url = os.environ["APP_DATABASE_URL"]
    app_eng = make_rls_engine(app_url)
    from persona_api.middleware.rls_context import current_user_id

    tok = current_user_id.set("user_B")
    try:
        with app_eng.begin() as conn:
            visible = conn.execute(
                text("SELECT count(*) FROM memory_chunks WHERE persona_id = :p"),
                {"p": ids_a["persona_id"]},
            ).scalar()
    finally:
        current_user_id.reset(tok)
    app_eng.dispose()
    su.dispose()
    assert visible == 0  # B cannot see A's persona's memory chunks


# ---------------------------------------------------------------------------
# Spec 15 T18: cross-tenant RLS sweep extension for /imagegen.
#
# Extends the spec-08 sweep + the spec-13 T11 uploads pair so the imagegen
# route is covered by the same RLS adversarial proof. Same shape as the
# uploads cross-tenant test (D-08-1 existence-disclosure-safe 404), plus
# the three Spec 15 invariants the imagegen route adds on top:
#
# 1. **No bytes written for B** — the route's pre-flight RLS persona check
#    fires before the service layer's workspace I/O, so user B's workspace
#    subtree (under A's persona id, or anywhere) stays empty.
# 2. **Credits for B NOT deducted** — the pre-flight 404 fires BEFORE the
#    route's ``require_credits`` gate AND before the service-layer
#    pre-deduct (D-15-X-pre-deduct-credits), so the credits ledger has no
#    new entries for B.
# 3. **No audit entry for B** — the route only emits ``imagegen.create`` on
#    the success branch (after a successful generation); the 404 path
#    short-circuits before any audit emission.
#
# These three invariants together prove "cross-tenant attack is structurally
# inert" — not just "the HTTP response is 404" but "no side effect on the
# system from the attempt." Mirrors the test_api_imagegen.py scenario-2
# pattern, but landed here in the per-endpoint sweep so the spec-08 RLS
# acceptance proof inherits the imagegen route.
# ---------------------------------------------------------------------------


class _NoOpImageBackend:
    """Fake :class:`ImageBackend` that fails loud if dispatched.

    The cross-tenant 404 must fire at the route's pre-flight RLS check —
    BEFORE any backend dispatch. If the test ever reaches ``generate()``
    the assertion below makes the failure obvious, rather than silently
    succeeding because B's request happens to fail later for an unrelated
    reason. Mirrors the ``_RejectingBackend`` shape from test_api_imagegen.
    """

    @property
    def provider_name(self) -> str:
        return "fake"

    @property
    def model_name(self) -> str:
        return "fake-no-op"

    async def generate(self, prompt: str, *, options: object | None = None) -> object:  # noqa: ARG002
        raise AssertionError(
            "imagegen backend reached on cross-tenant call — pre-flight RLS 404 should have fired"
        )

    async def edit(
        self,
        input_image: object,
        instructions: str,  # noqa: ARG002
        *,
        options: object | None = None,  # noqa: ARG002
    ) -> object:
        raise NotImplementedError("edit not supported in v1")


def test_user_b_cannot_post_imagegen_to_user_a_persona(
    migrated_engine: Engine,  # noqa: ARG001 — ensures schema + grants
    embedder: HashEmbedder384,
    tmp_path: object,
) -> None:
    """Spec 15 T18: cross-tenant POST to /personas/{id}/imagegen → 404.

    Two users, persona owned by A; B's POST returns 404 (existence-
    disclosure-safe per D-08-1 / D-15-X-workspace-coordination). The three
    Spec-15-specific invariants are asserted on top of the base 404:

    * No bytes written for B (workspace empty under B/A_persona_id).
    * No credits movement for B (no new credit_transactions row).
    * No audit entry for B (no imagegen.create row).
    """
    import os
    from pathlib import Path

    from persona_api.app import create_app
    from persona_api.config import APIConfig
    from persona_api.db.models import audit_log as audit_log_t
    from persona_api.db.models import credit_transactions as credit_tx_t
    from sqlalchemy import select

    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")
    # Cross-tenant isolation requires RLS to actually fire — superusers
    # bypass RLS per D-07-5. Skip cleanly when the test env is using the
    # superuser DSN; the structural invariant cannot be verified there.
    if "persona_app" not in app_url:
        pytest.skip(
            "APP_DATABASE_URL is not using the non-superuser persona_app role;"
            " RLS isolation cannot be verified (D-07-5)"
        )

    assert isinstance(tmp_path, Path)
    workspace_root = tmp_path / "workspace"
    cfg = APIConfig(
        app_database_url=app_url,
        audit_root=str(tmp_path / "audit"),
        workspace_root=workspace_root,
    )
    app = create_app(cfg)

    from persona_api.auth import AuthenticatedUser

    async def _verify(token: str) -> AuthenticatedUser:
        return AuthenticatedUser(id=token, email=None)

    async def _build(_pid: str) -> _Loop:
        return _Loop()

    with TestClient(app) as c:
        app.state.verify_token = _verify
        # Mirror the existing ``app_client`` fixture overrides so the
        # create-persona path doesn't pull a real chat backend (which
        # would 500 on missing ANTHROPIC_API_KEY in CI) and uses a fast
        # deterministic embedder for memory population.
        app.state.embedder = embedder
        app.state.build_conversation_loop = _build
        # Drop the runtime registry so the persona-detail response does
        # not lazily instantiate a chat backend (the registry's
        # ``supports_vision_for`` triggers a backend construction which
        # raises ``AuthenticationError("missing API key")`` when
        # ``ANTHROPIC_API_KEY`` is unset — pre-existing CI surface). The
        # ``_persona_detail`` helper treats a missing registry as
        # ``capabilities = None`` (test paths), so the 201 lands cleanly.
        if hasattr(app.state, "tier_registry"):
            app.state.tier_registry = None
        # Install a fake backend that loud-fails if reached. Cross-tenant
        # 404 must fire BEFORE dispatch — this is the structural guarantee.
        app.state.image_backend = _NoOpImageBackend()
        su = make_rls_engine(os.environ["DATABASE_URL"])
        try:
            # Seed both users under the superuser engine (FK target for
            # personas.owner_id + credits.user_id).
            with su.begin() as conn:
                for u in ("user_A", "user_B"):
                    conn.execute(
                        text(
                            "INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"
                        ),
                        {"i": u, "e": f"{u}@x.test"},
                    )

            # A creates a persona. The body is the minimal valid YAML from
            # the existing sweep above; visual_style left unset (T10 made
            # it additive + optional).
            pid = c.post("/v1/personas", json={"yaml": _YAML}, headers=_h("user_A")).json()["id"]

            # Snapshot the credit_transactions + audit_log state for B
            # before the cross-tenant attempt. Both should be unchanged
            # afterward.
            with su.begin() as conn:
                pre_deltas = [
                    int(r[0])
                    for r in conn.execute(
                        select(credit_tx_t.c.delta).where(credit_tx_t.c.user_id == "user_B")
                    ).all()
                ]
                pre_actions = [
                    str(r[0])
                    for r in conn.execute(
                        select(audit_log_t.c.action).where(audit_log_t.c.user_id == "user_B")
                    ).all()
                ]

            # B's cross-tenant POST → 404 (existence-disclosure-safe).
            resp = c.post(
                f"/v1/personas/{pid}/imagegen",
                json={"prompt": "a red bicycle", "size": "1024x1024", "count": 1},
                headers=_h("user_B"),
            )
            assert resp.status_code == 404, resp.text

            # Invariant 1: no bytes for B anywhere under the workspace.
            # The route 404s before any I/O — neither B/A_persona_id/uploads
            # nor any B subtree should exist with content. ``glob('**/*')``
            # walks the whole workspace and asserts no files (directories
            # may exist from the app's ``workspace_root.mkdir`` at startup,
            # but no file bytes).
            user_b_root = workspace_root / "user_B"
            if user_b_root.exists():
                files = [p for p in user_b_root.rglob("*") if p.is_file()]
                assert files == [], f"expected no bytes for user B, got {files}"
            # Defensive: confirm B/A_persona_id specifically is empty too.
            user_b_a_persona = workspace_root / "user_B" / pid
            if user_b_a_persona.exists():
                files = [p for p in user_b_a_persona.rglob("*") if p.is_file()]
                assert files == [], f"expected no bytes at workspace/user_B/{pid}, got {files}"

            # Invariant 2: no credits movement for B (the pre-flight 404
            # fires before require_credits + before service-layer
            # pre-deduct). The ledger row count for B is unchanged.
            with su.begin() as conn:
                post_deltas = [
                    int(r[0])
                    for r in conn.execute(
                        select(credit_tx_t.c.delta).where(credit_tx_t.c.user_id == "user_B")
                    ).all()
                ]
            assert post_deltas == pre_deltas, (
                f"expected no credit movement for user B, got pre={pre_deltas} post={post_deltas}"
            )

            # Invariant 3: no audit entry for B against ``imagegen.create``
            # (the API-layer audit only fires on the success branch in
            # routes/imagegen.py; the 404 short-circuits before it).
            with su.begin() as conn:
                post_actions = [
                    str(r[0])
                    for r in conn.execute(
                        select(audit_log_t.c.action).where(audit_log_t.c.user_id == "user_B")
                    ).all()
                ]
            assert "imagegen.create" not in post_actions, (
                f"expected no imagegen.create audit row for user B, got {post_actions}"
            )
            # And nothing new at all on the imagegen action for B.
            new_actions = [a for a in post_actions if a not in pre_actions]
            assert "imagegen.create" not in new_actions

        finally:
            with su.begin() as conn:
                conn.execute(text("DELETE FROM users WHERE id IN ('user_A','user_B')"))
            su.dispose()
