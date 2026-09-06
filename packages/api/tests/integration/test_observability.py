"""Observability: turn_logs, credits, audit_log, health (spec 08, T12).

Drives the real app + Docker Postgres with a fake verifier + scripted loop (the
loop writes a TurnLog via the injected PostgresTurnLogWriter). Asserts: a turn
deducts credits + writes a turn_log; /me/credits reflects the deduction; a failed
turn doesn't deduct; audit_log rows on persona create; /healthz 200.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from persona.backends import StreamChunk, TokenUsage
from persona.schema.conversation import ConversationMessage
from persona_api.app import create_app
from persona_api.config import APIConfig
from persona_api.middleware.rls_context import make_rls_engine
from persona_api.services.turn_log_writer import PostgresTurnLogWriter
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
    """Scripted loop that writes a TurnLog (mimicking the real loop) then streams."""

    def __init__(
        self, conversation_id: str, writer: PostgresTurnLogWriter, *, fail: bool = False
    ) -> None:
        self._cid = conversation_id
        self._writer = writer
        self._fail = fail

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
        if self._fail:
            raise RuntimeError("boom")  # a failed turn: no persist, no deduct
        conversation.messages.append(
            ConversationMessage(role="assistant", content="ok", created_at=now)
        )
        from persona_runtime.logging import TurnLog

        self._writer.write(
            TurnLog(
                conversation_id=conversation.conversation_id,
                turn_index=0,
                tier_used="frontier",
                model_name="scripted",
                provider="anthropic",
                prompt_tokens=10,
                completion_tokens=5,
                latency_ms=12.0,
                cost_cents=0.1,
                tool_calls=0,
                skill_used=None,
                history_compacted=False,
                timestamp=now,
            )
        )
        yield StreamChunk(
            delta="ok",
            is_final=True,
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )


@pytest.fixture
def ctx(
    migrated_engine: Engine,  # noqa: ARG001
    embedder: HashEmbedder384,
    tmp_path: object,
) -> Iterator[tuple[TestClient, str, str, Engine]]:
    import os

    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")
    cfg = APIConfig(
        app_database_url=app_url, audit_root=str(tmp_path) + "/audit", credits_per_turn=7
    )
    app = create_app(cfg)
    rls = make_rls_engine(app_url)
    writer = PostgresTurnLogWriter(rls)

    from persona_api.auth import AuthenticatedUser

    async def _verify(token: str) -> AuthenticatedUser:
        return AuthenticatedUser(id=token, email=None)

    uid = "user_t12"
    with TestClient(app) as c:
        app.state.verify_token = _verify
        app.state.embedder = embedder
        # Drop the lifespan-installed TierRegistry so the persona-detail
        # capabilities surface doesn't lazily instantiate a real chat backend
        # (AuthenticationError("missing API key") on CI without ANTHROPIC_API_KEY).
        if hasattr(app.state, "tier_registry"):
            app.state.tier_registry = None
        su = make_rls_engine(os.environ["DATABASE_URL"])
        with su.begin() as conn:
            conn.execute(
                text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
                {"i": uid, "e": f"{uid}@x"},
            )
        su.dispose()
        pid = c.post(
            "/v1/personas", json={"yaml": _YAML}, headers={"Authorization": f"Bearer {uid}"}
        ).json()["id"]
        conv = c.post(
            f"/v1/personas/{pid}/conversations",
            json={"title": "t"},
            headers={"Authorization": f"Bearer {uid}"},
        ).json()["id"]

        async def _build(_pid: str) -> _Loop:
            return _Loop(conv, writer)

        app.state.build_conversation_loop = _build
        yield c, uid, conv, rls
        rls.dispose()
        su = make_rls_engine(os.environ["DATABASE_URL"])
        with su.begin() as conn:
            conn.execute(text("DELETE FROM users WHERE id = :i"), {"i": uid})
        su.dispose()


def _auth(uid: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {uid}"}


def test_turn_deducts_credits_and_writes_turn_log(
    ctx: tuple[TestClient, str, str, Engine],
) -> None:
    c, uid, conv, rls = ctx
    before = c.get("/v1/me/credits", headers=_auth(uid)).json()["balance"]

    r = c.post(f"/v1/conversations/{conv}/messages", json={"content": "hi"}, headers=_auth(uid))
    assert r.status_code == 200
    # drain the stream so persist-after-final + deduction run
    _ = r.text

    after = c.get("/v1/me/credits", headers=_auth(uid)).json()["balance"]
    assert after == before - 7  # credits_per_turn=7

    # a turn_log row exists for this conversation
    with rls.begin() as conn:
        conn.execute(text("SELECT set_config('app.current_user_id', :u, false)"), {"u": uid})
        count = conn.execute(
            text("SELECT count(*) FROM turn_logs WHERE conversation_id = :c"), {"c": conv}
        ).scalar()
    assert count == 1

    # /me/usage surfaces the turn
    usage = c.get("/v1/me/usage", headers=_auth(uid)).json()
    assert len(usage) == 1
    assert usage[0]["tier_used"] == "frontier"
    assert usage[0]["prompt_tokens"] == 10


def test_failed_turn_does_not_deduct(ctx: tuple[TestClient, str, str, Engine]) -> None:
    c, uid, conv, rls = ctx

    async def _build_fail(_pid: str) -> _Loop:
        return _Loop(conv, PostgresTurnLogWriter(rls), fail=True)

    c.app.state.build_conversation_loop = _build_fail  # type: ignore[attr-defined]
    before = c.get("/v1/me/credits", headers=_auth(uid)).json()["balance"]
    # P1 background-worker model: the loop raises mid-stream, but the worker
    # catches it (a background task never crashes silently), finalizes
    # status="error" — which skips the deduction — and surfaces the failure as an
    # ``event: error`` frame. The POST itself returns 200; the failure rides the
    # stream rather than propagating as an exception.
    r = c.post(f"/v1/conversations/{conv}/messages", json={"content": "hi"}, headers=_auth(uid))
    assert r.status_code == 200
    assert "turn_failed" in r.text  # the failure rode the stream (no silent success)
    after = c.get("/v1/me/credits", headers=_auth(uid)).json()["balance"]
    assert after == before  # no deduction on failure — the invariant


def test_audit_log_on_persona_create(ctx: tuple[TestClient, str, str, Engine]) -> None:
    c, uid, _conv, _rls = ctx
    # the fixture already created a persona; check an audit row exists (audit_log
    # is not RLS-scoped — query as superuser)
    import os

    su = make_rls_engine(os.environ["DATABASE_URL"])
    with su.begin() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM audit_log WHERE user_id = :u AND action = 'persona.create'"),
            {"u": uid},
        ).scalar()
    su.dispose()
    assert count is not None
    assert count >= 1


def test_healthz_ok(ctx: tuple[TestClient, str, str, Engine]) -> None:
    c, _uid, _conv, _rls = ctx
    r = c.get("/healthz")
    assert r.status_code == 200
    # R9-093 put worker liveness on /healthz; this fixture starts no in-process
    # worker, so the honest value is "absent" (not "stale", which would be a 503).
    assert r.json() == {"status": "ok", "db": "connected", "worker": "absent"}
