"""The A6 chat-twin seam — a decision reply routes to the resolver (Spec A6, chat-twin).

Drives the pre-turn detection helper (`_maybe_resolve_pending_approval`) against a real DB with a
stub resolution service, so the seam's routing is proven without running a model turn:

- a **cued** reply (deterministic) + a **pending approval** on the conversation → routed to
  `resolve(channel="chat")`, the user's reply persisted, a minimal resolution SSE returned;
- a **non-cued** reply → `None` (the caller runs a normal turn; the proposal stays pending);
- a **cued** reply with **nothing pending** → `None`.

The resolve semantics themselves (verbatim replay, material-modify re-confirm, dual-resolution
exactly-one-winner) are proven in the resolver / service tests — not re-exercised here.
"""

# ruff: noqa: ARG001 — ``migrated_engine`` is a dependency-ordering fixture param.
from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from persona.approvals import ActionProposal, DecisionType
from persona.tools import ActionCategory
from persona_api.approvals import ApprovalStore
from persona_api.approvals.resolver import ResolutionOutcome
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from persona_api.routes.conversations import _maybe_resolve_pending_approval
from persona_api.services.chat_turn_sink import MessagesTurnSink
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_CONTRACT = '\'{"goal": "x"}\''


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Iterator[Engine]:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL (non-superuser role) not set; skipping chat-twin test")
    engine = create_engine(app_url.replace("+asyncpg", "+psycopg"))
    yield engine
    engine.dispose()


def _seed_pending(su: Engine, app_engine: Engine) -> None:
    with su.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email) VALUES ('u','u@x')"))
        conn.execute(text("INSERT INTO personas (id, owner_id, yaml) VALUES ('p','u','name: x')"))
        conn.execute(
            text("INSERT INTO conversations (id, owner_id, persona_id) VALUES ('c','u','p')")
        )
        conn.execute(
            text(
                "INSERT INTO tasks (id, owner_id, persona_id, contract_json, conversation_id, "
                f"state, wait_kind) VALUES ('t','u','p',{_CONTRACT}::jsonb,'c','waiting','on_user')"
            )
        )
    ApprovalStore(app_engine).create_proposal(
        ActionProposal(
            proposal_id="pid",
            owner_id="u",
            task_id="t",
            persona_id="p",
            categories=frozenset({ActionCategory.COMMUNICATE_AS_USER}),
            tool_name="send_email",
            arguments={"to": "bob@example.com"},
            description="Send an email to bob@example.com",
            created_at=datetime.now(UTC),
        )
    )


class _StubService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, str]] = []

    async def resolve(
        self,
        owner_id: str,
        proposal_id: str,
        reply: str,
        channel: str,
        *,
        now: datetime,  # noqa: ARG002
    ) -> ResolutionOutcome:
        self.calls.append((owner_id, proposal_id, reply, channel))
        return ResolutionOutcome(outcome=DecisionType.APPROVE, executed=True, resumed=True)


def _request(rls_engine: Engine, service: _StubService) -> SimpleNamespace:
    # rls_engine is a make_rls_engine (the checkout listener applies current_user_id) so the sink's
    # message write is owner-scoped exactly as it is in a real request (the middleware binds it).
    state = SimpleNamespace(
        rls_engine=rls_engine,
        build_approval_resolver=lambda: service,
        chat_turn_sink=MessagesTurnSink(rls_engine),
    )
    return SimpleNamespace(app=SimpleNamespace(state=state))


async def test_cued_reply_with_pending_approval_routes_to_resolver(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    _seed_pending(migrated_engine, app_engine)
    service = _StubService()
    rls_engine = make_rls_engine(os.environ["APP_DATABASE_URL"])
    token = current_user_id.set("u")  # the request's RLS scope (middleware does this in prod)
    try:
        resp = await _maybe_resolve_pending_approval(
            _request(rls_engine, service),
            owner_id="u",
            conversation_id="c",
            reply="yes",  # type: ignore[arg-type]
        )
        assert resp is not None  # a minimal resolution SSE, not a normal turn
        assert service.calls == [("u", "pid", "yes", "chat")]  # same service, channel="chat"
        # the user's reply is persisted to the transcript (read via the superuser engine).
        with migrated_engine.connect() as conn:
            rows = conn.execute(
                text("SELECT content FROM messages WHERE conversation_id='c' AND role='user'")
            ).all()
        assert [r.content for r in rows] == ["yes"]
    finally:
        current_user_id.reset(token)
        rls_engine.dispose()


async def test_non_cued_reply_falls_through_to_normal_turn(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    _seed_pending(migrated_engine, app_engine)
    service = _StubService()

    resp = await _maybe_resolve_pending_approval(
        _request(app_engine, service),
        owner_id="u",
        conversation_id="c",
        reply="what's the weather in Bergen?",  # type: ignore[arg-type]
    )

    assert resp is None  # a normal chat turn runs; the proposal stays pending
    assert service.calls == []
    assert ApprovalStore(app_engine).get_pending_for_conversation("u", "c") is not None


async def test_cued_reply_with_nothing_pending_falls_through(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    with migrated_engine.begin() as conn:  # a conversation with NO pending approval
        conn.execute(text("INSERT INTO users (id, email) VALUES ('u','u@x')"))
        conn.execute(text("INSERT INTO personas (id, owner_id, yaml) VALUES ('p','u','name: x')"))
        conn.execute(
            text("INSERT INTO conversations (id, owner_id, persona_id) VALUES ('c','u','p')")
        )
    service = _StubService()

    resp = await _maybe_resolve_pending_approval(
        _request(app_engine, service),
        owner_id="u",
        conversation_id="c",
        reply="yes",  # type: ignore[arg-type]
    )

    assert resp is None
    assert service.calls == []
