"""The shared approval-resolution service, end-to-end on REAL components (Spec A6, T-seam).

Completes A3's loop through the ONE live path A6 stands up. No fakes for the load-bearing edges:

- **Real executor** — a real :class:`RuntimeFactory` builds the persona's real, un-gated toolbox
  (``build_action_executor``); approving a ``task_introspect(task_id="t1")`` proposal dispatches it
  verbatim, so the resolution checkpoint carries t1's own state (``Task: win the appeal``) — the
  EXACT recorded arg reached the real tool; the model never re-derived it.
- **Real continuation** — the task resumes through the real ``TaskContinuation`` + ``JobQueue``.
- **Two-layer CAS at-most-once** — a second resolve of the (now consumed) proposal is a clean
  ``not_pending`` no-op; no second execution, no second resolution checkpoint.
- **Real notifier** — ``announce`` posts a persona-voiced C0 ask into the task's conversation via
  the real :class:`Originator` (a durable ``messages`` row appears).

``task_introspect`` is chosen deliberately: it is unconditionally composed, deterministic, needs no
model/sandbox, and its output reflects its arg — an honest, side-effect-free probe of verbatim
replay through the real toolbox.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from persona.approvals import ActionProposal, ProposalStatus
from persona.stores.postgres import PostgresBackend
from persona.tasks import Contract, Task, WaitKind
from persona.tools import ActionCategory
from persona_api.approvals import ApprovalStore
from persona_api.config import Edition
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from persona_api.services.approval_resolution_service import ApprovalResolutionService
from persona_api.services.runtime_factory import RuntimeFactory
from persona_api.tasks.store import CheckpointStore, TaskStore
from sqlalchemy import select, text

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from persona.backends import StreamChunk
    from persona.schema.conversation import ConversationMessage
    from tests.conftest import HashEmbedder384

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_NOW = datetime(2026, 7, 6, 9, 0, tzinfo=UTC)
_YAML = """\
schema_version: "1.0"
identity:
  name: Astrid
  role: assistant
  background: |
    A helper.
  language_default: en
  constraints: []
tools:
  - task_introspect
"""


class _StubBackend:
    provider_name = "anthropic"
    model_name = "scripted"
    max_tokens = 4096

    @property
    def supports_native_tools(self) -> bool:
        return True

    @property
    def supports_vision(self) -> bool:
        return False

    async def chat(self, messages: list[ConversationMessage], **_: object) -> object:  # noqa: ARG002
        raise NotImplementedError

    async def chat_stream(
        self,
        messages: list[ConversationMessage],  # noqa: ARG002
        **_: object,
    ) -> AsyncIterator[StreamChunk]:
        return
        yield  # unreachable — makes this an (unused) async generator


class _Registry:
    """A minimal TierRegistry double (no model is exercised — task_introspect is model-free)."""

    def __init__(self) -> None:
        self._b = _StubBackend()

    def get(self, _tier: str) -> _StubBackend:
        return self._b

    @property
    def configured_tier_names(self) -> tuple[str, ...]:
        return ("frontier", "mid", "small")

    def supports_vision_for(self, _tier: str) -> bool:
        return False

    def metadata_for(self, _tier: str) -> None:
        return None

    def model_name_for(self, _tier: str) -> str:
        return "scripted"

    async def aclose(self) -> None:
        pass


class _NullTurnLog:
    def write(self, _log: object) -> None:
        pass


def _seed(su_url: str, owner: str, persona: str, conversation: str) -> None:
    su = make_rls_engine(su_url)
    with su.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
            {"i": owner, "e": f"{owner}@x"},
        )
        conn.execute(
            text("INSERT INTO personas (id, owner_id, yaml) VALUES (:i, :o, :y)"),
            {"i": persona, "o": owner, "y": _YAML},
        )
        conn.execute(
            text("INSERT INTO conversations (id, owner_id, persona_id) VALUES (:c, :o, :p)"),
            {"c": conversation, "o": owner, "p": persona},
        )
    su.dispose()


def _park_task(tasks: TaskStore, owner: str, persona: str, task_id: str, conv: str) -> None:
    tasks.create(
        Task(
            id=task_id,
            owner_id=owner,
            persona_id=persona,
            contract=Contract(goal="win the appeal"),
            conversation_id=conv,
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    tasks.start(owner, task_id, now=_NOW)
    tasks.begin_wait(owner, task_id, WaitKind.ON_USER, now=_NOW)


def _proposal(owner: str, persona: str, task_id: str, pid: str) -> ActionProposal:
    return ActionProposal(
        proposal_id=pid,
        owner_id=owner,
        task_id=task_id,
        persona_id=persona,
        categories=frozenset({ActionCategory.OBSERVE}),
        tool_name="task_introspect",
        arguments={"task_id": task_id},
        description=f"Introspect task {task_id}",
        created_at=_NOW,
    )


def _service(rls_engine: object, embedder: HashEmbedder384) -> ApprovalResolutionService:
    factory = RuntimeFactory(
        rls_engine=rls_engine,  # type: ignore[arg-type]
        embedder=embedder,
        tier_registry=_Registry(),  # type: ignore[arg-type]
        turn_log_writer=_NullTurnLog(),  # type: ignore[arg-type]
        audit_root=Path("/tmp/persona-a6-tseam-audit"),
    )
    return ApprovalResolutionService(
        engine=rls_engine,  # type: ignore[arg-type]
        factory=factory,
        edition=Edition.community,
        memory_backend=PostgresBackend(engine=rls_engine, embedder=embedder),  # type: ignore[arg-type]
        audit_root=Path("/tmp/persona-a6-tseam-audit"),
    )


async def test_service_resolves_a_real_proposal_verbatim_and_is_at_most_once(
    migrated_engine: object,  # noqa: ARG001 — dependency-ordering fixture
    embedder: HashEmbedder384,
) -> None:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")
    su_url = os.environ["DATABASE_URL"]
    owner, persona, conv = "u_ts", "p_ts", "c_ts"
    _seed(su_url, owner, persona, conv)

    rls_engine = make_rls_engine(app_url)
    token = current_user_id.set(owner)
    try:
        tasks = TaskStore(rls_engine)
        _park_task(tasks, owner, persona, "t1", conv)
        ApprovalStore(rls_engine).create_proposal(_proposal(owner, persona, "t1", "p1"))
        service = _service(rls_engine, embedder)

        # (1) APPROVE — the real factory-built, un-gated executor replays the EXACT recorded call.
        outcome = await service.resolve(owner, "p1", "yes", "web", now=_NOW)
        assert outcome.executed is True
        assert outcome.outcome is not None
        assert outcome.outcome.value == "approve"

        # verbatim: task_introspect ran with the EXACT task_id="t1" → its own goal is in the
        # resolution checkpoint (the model never re-derived the call).
        conclusion = CheckpointStore(rls_engine).get_latest(owner, "t1").progress_conclusions[-1]
        assert conclusion.startswith("Approved + executed:")
        assert "win the appeal" in conclusion  # t1's real, introspected state

        # the durable A3 record is the anchor — the proposal is consumed exactly once.
        consumed = ApprovalStore(rls_engine).get_proposal(owner, "p1").status
        assert consumed is ProposalStatus.CONSUMED
        after_first = len(CheckpointStore(rls_engine).list_recent(owner, "t1", limit=50))

        # (2) AT-MOST-ONCE — a second resolve of the consumed proposal is a clean no-op.
        again = await service.resolve(owner, "p1", "yes", "web", now=_NOW)
        assert again.outcome is None
        assert again.note == "not_pending"
        after_second = len(CheckpointStore(rls_engine).list_recent(owner, "t1", limit=50))
        assert after_second == after_first  # no second execution, no second checkpoint
    finally:
        current_user_id.reset(token)
        rls_engine.dispose()


async def test_service_notifier_posts_a_real_c0_ask(
    migrated_engine: object,  # noqa: ARG001
    embedder: HashEmbedder384,
) -> None:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")
    su_url = os.environ["DATABASE_URL"]
    owner, persona, conv = "u_ask", "p_ask", "c_ask"
    _seed(su_url, owner, persona, conv)

    rls_engine = make_rls_engine(app_url)
    token = current_user_id.set(owner)
    try:
        _park_task(TaskStore(rls_engine), owner, persona, "t2", conv)
        ApprovalStore(rls_engine).create_proposal(_proposal(owner, persona, "t2", "p2"))
        service = _service(rls_engine, embedder)

        # announce → the REAL Originator notifier posts a persona-voiced ask on the conversation.
        resolver = service.build_resolver(persona)
        await resolver.announce(owner, "p2")

        from persona_api.db.models import messages as messages_t

        with rls_engine.connect() as conn:
            rows = conn.execute(
                select(messages_t.c.content).where(messages_t.c.conversation_id == conv)
            ).all()
        contents = [r.content for r in rows]
        assert any("introspect task t2" in c.lower() for c in contents), contents
    finally:
        current_user_id.reset(token)
        rls_engine.dispose()
