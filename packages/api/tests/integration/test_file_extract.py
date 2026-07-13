"""R9-025b — Turn-into-file, end-to-end against real Postgres.

The A4/V8 rule: drive the REAL trigger chain, no hand-forced job payloads. A
real completed chat turn (the real HTTP route → the scripted conversation
loop → real persisted messages) produces a real assistant message; the real
``POST .../turn-into-file`` route enqueues a real ``file_extract`` job; a real
A0 ``Worker`` (real ``JobQueue`` claim → real ``PgFileExtractRepository`` SQL)
executes it on a SCRIPTED LLM extractor + a SCRIPTED ``CodeSandbox`` boundary
(wrapped in a REAL ``SandboxPool`` — the plumbing/substrate, not the model or
E2B, per the provider-adapter-live-leg rule: this proves the machinery, a live
E2B/model leg is a separate owner-run concern).

Covered here:
- the route enqueues exactly one job keyed ``file_extract:{message_id}:auto``
  and returns 202 + a job reference;
- the real worker runs the handler: a real file lands on disk under the
  persona workspace with a real F5 sidecar, is listed by
  ``GET /v1/personas/{id}/artifacts``, and ``sidebar.changed``
  (reason=conversation.file_extracted) is published;
- a same-(message,format) re-POST is A0's dedup no-op (``job_id=None``), and a
  re-delivered run (fresh key, same payload) converges — no duplicate artifact;
- a user-message target 422s (never enqueued);
- a cross-tenant conversation_id 404s.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from persona.backends import StreamChunk, TokenUsage
from persona.jobs import JobRegistry
from persona.sandbox.result import ExecutionResult, NetworkPolicy, ResourceLimits, SandboxFile
from persona.schema.conversation import ConversationMessage
from persona_api.app import create_app
from persona_api.auth import AuthenticatedUser
from persona_api.config import APIConfig
from persona_api.jobs import JobQueue, Worker
from persona_api.jobs.handlers.file_extract import (
    ExtractedContent,
    SandboxFileRenderer,
    register_file_extract_handler,
)
from persona_api.middleware.rls_context import make_rls_engine
from persona_api.sandbox.pool import SandboxPool
from persona_api.services.artifact_metadata import read_artifact_sidecar
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterator

    from persona.schema.conversation import Conversation
    from persona_runtime.prompt import DocumentContext
    from sqlalchemy import Engine
    from tests.conftest import HashEmbedder384

pytestmark = pytest.mark.integration

_VALID_YAML = """\
schema_version: "1.0"
identity:
  name: Astrid
  role: assistant
  background: |
    A helper.
  language_default: en
  constraints: []
"""

_EXTRACTED_TITLE = "Board Game Night Plan"
_EXTRACTED_REF = "board-game-night-plan.pdf"


class _ScriptedLoop:
    """The test_conversations stand-in: streams a reply + mutates the conversation
    exactly as the real loop does (appends user + assistant on success)."""

    def __init__(self, reply: str = "Weekly board games, rotating hosts.") -> None:
        self._reply = reply

    async def turn(
        self,
        conversation: Conversation,
        user_message: str,
        on_event: Callable[[object], Awaitable[None]] | None = None,  # noqa: ARG002
        *,
        turn_has_image: bool = False,  # noqa: ARG002 — real-loop kwarg compat
        images: list[object] | None = None,  # noqa: ARG002 — real-loop kwarg compat
        documents: list[object] | None = None,  # noqa: ARG002 — real-loop kwarg compat
        document_context: DocumentContext | None = None,  # noqa: ARG002 — real-loop kwarg compat
    ) -> AsyncIterator[StreamChunk]:
        from datetime import UTC, datetime  # noqa: PLC0415

        now = datetime.now(UTC)
        conversation.messages.append(
            ConversationMessage(role="user", content=user_message, created_at=now)
        )
        yield StreamChunk(delta=self._reply, is_final=False)
        conversation.messages.append(
            ConversationMessage(role="assistant", content=self._reply, created_at=now)
        )
        yield StreamChunk(
            delta="",
            is_final=True,
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )


class _RecordingChannel:
    """Duck-typed UserEventChannel: records every publish (owner, event)."""

    def __init__(self) -> None:
        self.published: list[tuple[str, object]] = []

    def publish(self, owner_id: str, event: object) -> None:
        self.published.append((owner_id, event))


class _FakeCodeSandbox:
    """Scripted CodeSandbox — the doc-gen sandbox boundary, per the kickoff's
    "boundary SCRIPTED in tests" rule. Always produces ``_EXTRACTED_REF`` with
    real bytes on ``copy_produced_file_to`` (mirrors LocalDockerSandbox's real
    disk-to-disk copy contract, just against an in-memory byte source)."""

    def __init__(self) -> None:
        self.execute_calls: list[dict[str, object]] = []

    async def execute(
        self,
        code: str,
        *,
        language: str = "python",  # noqa: ARG002
        session_id: str | None = None,
        timeout_s: float = 30.0,  # noqa: ARG002
        limits: ResourceLimits | None = None,  # noqa: ARG002
        network: NetworkPolicy | None = None,  # noqa: ARG002
        input_files: list[SandboxFile] | None = None,  # noqa: ARG002
    ) -> ExecutionResult:
        self.execute_calls.append({"code": code, "session_id": session_id})
        return ExecutionResult(
            stdout="",
            stderr="",
            exit_status=0,
            outcome="ok",
            produced_files=(
                SandboxFile(path=_EXTRACTED_REF, size_bytes=9, media_type="application/pdf"),
            ),
        )

    async def create_session(
        self,
        session_id: str,  # noqa: ARG002
        *,
        limits: ResourceLimits,  # noqa: ARG002
        network: NetworkPolicy,  # noqa: ARG002
    ) -> None:
        return

    async def destroy_session(self, session_id: str) -> None:  # noqa: ARG002
        return

    async def aclose(self) -> None:
        return

    async def copy_produced_file_to(self, session_id: str, ref: str, target_path: Path) -> None:  # noqa: ARG002
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(b"%PDF-fake")

    async def read_produced_file_bytes(self, session_id: str, ref: str) -> bytes:  # noqa: ARG002
        return b"%PDF-fake"


class _StubTierRegistry:
    """Minimal TierRegistry-shaped stand-in for a genuinely keyless test env.

    Only used when ``app.state.tier_registry`` is ``None`` (no model key
    configured at all) — preserves a REAL registry when the environment
    happens to have one (this repo's default test env may). Duck-types just
    enough (``configured_tier_names`` / ``supports_vision_for``) for the
    OTHER routes this test's flow touches (persona create's capability
    hydration, ``personas.py::_capabilities_from_registry``) not to crash on
    a real attribute access — the route under test itself only checks
    ``is not None``.
    """

    configured_tier_names: tuple[str, ...] = ()

    def supports_vision_for(self, _name: str) -> bool:
        return False


async def _extractor(_prompt: list[ConversationMessage]) -> ExtractedContent | None:
    return ExtractedContent(
        title=_EXTRACTED_TITLE,
        kind="prose",  # type: ignore[arg-type]
        body_markdown="Weekly, rotating hosts, bring snacks.",
    )


@pytest.fixture
def client(
    migrated_engine: Engine,  # noqa: ARG001 — schema + grants
    embedder: HashEmbedder384,
    tmp_path: Path,
) -> Iterator[tuple[TestClient, str, str, Path]]:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")
    workspace_root = tmp_path / "workspace"
    cfg = APIConfig(
        app_database_url=app_url,
        audit_root=str(tmp_path) + "/audit",
        workspace_root=str(workspace_root),  # type: ignore[arg-type]
    )
    app = create_app(cfg)

    async def _fake_verify(token: str) -> AuthenticatedUser:
        return AuthenticatedUser(id=token, email=None)

    async def _build_loop(_persona_id: str) -> _ScriptedLoop:
        return _ScriptedLoop()

    user_id = "user_r9025b"
    with TestClient(app) as c:
        app.state.verify_token = _fake_verify
        app.state.embedder = embedder
        app.state.build_conversation_loop = _build_loop
        # The route's file_extract_queue_ready gate only checks PRESENCE. Preserve
        # a REAL tier_registry if this env happens to have a model key configured
        # (a bare sentinel there crashes persona-create's capability hydration,
        # which reads real TierRegistry attributes); fall back to a minimal stand-
        # in only when genuinely keyless, so the gate is deterministic either way.
        if getattr(app.state, "tier_registry", None) is None:
            app.state.tier_registry = _StubTierRegistry()
        app.state.sandbox_pool = object()
        su = make_rls_engine(os.environ["DATABASE_URL"])
        with su.begin() as conn:
            conn.execute(
                text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
                {"i": user_id, "e": f"{user_id}@x.test"},
            )
        su.dispose()
        resp = c.post(
            "/v1/personas",
            json={"yaml": _VALID_YAML},
            headers={"Authorization": f"Bearer {user_id}"},
        )
        persona_id = resp.json()["id"]
        yield c, user_id, persona_id, workspace_root
        su = make_rls_engine(os.environ["DATABASE_URL"])
        with su.begin() as conn:
            conn.execute(text("DELETE FROM users WHERE id = :i"), {"i": user_id})
        su.dispose()


def _auth(uid: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {uid}"}


def _new_conversation(c: TestClient, uid: str, persona_id: str) -> str:
    resp = c.post(
        f"/v1/personas/{persona_id}/conversations", json={"title": ""}, headers=_auth(uid)
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _turn(c: TestClient, uid: str, conv_id: str, content: str) -> None:
    resp = c.post(
        f"/v1/conversations/{conv_id}/messages", json={"content": content}, headers=_auth(uid)
    )
    assert resp.status_code == 200, resp.text
    _ = resp.text  # drain the SSE — the detached worker has finalized by EOS


def _message_id(su: Engine, conv_id: str, *, role: str) -> str:
    with su.begin() as conn:
        return str(
            conn.execute(
                text(
                    "SELECT id FROM messages WHERE conversation_id = :c AND role = :r "
                    "ORDER BY created_at DESC LIMIT 1"
                ),
                {"c": conv_id, "r": role},
            ).scalar_one()
        )


def _file_extract_jobs(su: Engine, uid: str) -> list[tuple[str, str]]:
    with su.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT idempotency_key, state FROM jobs "
                "WHERE type = 'file_extract' AND owner_id = :o ORDER BY created_at"
            ),
            {"o": uid},
        ).all()
    return [(r.idempotency_key, r.state) for r in rows]


def _prune_other_jobs(su: Engine, uid: str) -> None:
    """Harness hygiene: this test's registry serves ONLY file_extract — drop the
    other real enqueues (synthesis/episodic/title_refresh) so run_once claims
    deterministically. The file_extract rows themselves came from the REAL
    route (the trigger chain the kickoff requires)."""
    with su.begin() as conn:
        conn.execute(
            text("DELETE FROM jobs WHERE owner_id = :o AND type <> 'file_extract'"), {"o": uid}
        )


def _file_extract_worker(
    su: Engine,
    app_rls_engine: Engine,
    workspace_root: Path,
    channel: _RecordingChannel | None,
    sandbox: _FakeCodeSandbox,
) -> Worker:
    registry = JobRegistry()
    pool = SandboxPool(sandbox=sandbox)
    register_file_extract_handler(
        registry,
        extractor=_extractor,
        renderer=SandboxFileRenderer(pool=pool, workspace_root=workspace_root),
        event_channel=channel,  # type: ignore[arg-type]
    )
    return Worker(
        dispatch_engine=su,
        rls_engine=app_rls_engine,
        registry=registry,
        worker_id="w-file-extract",
    )


# ----- the real trigger chain: the route enqueues exactly one job -------------


def test_route_enqueues_exactly_one_file_extract_job(
    client: tuple[TestClient, str, str, Path],
) -> None:
    c, uid, persona_id, _workspace_root = client
    conv_id = _new_conversation(c, uid, persona_id)
    _turn(c, uid, conv_id, "plan a weekly board game night")
    su = make_rls_engine(os.environ["DATABASE_URL"])
    try:
        message_id = _message_id(su, conv_id, role="assistant")
        resp = c.post(
            f"/v1/conversations/{conv_id}/messages/{message_id}/turn-into-file",
            json={"format": "auto"},
            headers=_auth(uid),
        )
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["status"] == "queued"
        assert body["job_id"] is not None

        jobs = _file_extract_jobs(su, uid)
        assert [k for k, _s in jobs] == [f"file_extract:{message_id}:auto"]
    finally:
        su.dispose()


# ----- the real worker executes + persists + lists + publishes ----------------


def test_worker_produces_the_artifact_lists_it_and_publishes(
    client: tuple[TestClient, str, str, Path],
) -> None:
    """R9-025 reopen leg B: the produced file must land where BOTH surfaces
    read it from — GET /v1/conversations/{id}/documents (the surface this
    reopen's diagnosis named) AND GET /v1/personas/{id}/artifacts (what
    packages/web/src/components/chat/conversation-files.tsx's
    ``useConversationArtifacts`` hook actually calls — verified by reading the
    current web code: ``ConversationFiles`` is the chat header's Files button,
    and it is wired to the F5 artifacts endpoint, NOT the documents endpoint;
    ``useConversationDocuments`` exists but only tracks documents ATTACHED to
    the composer's next outgoing message — chat-window.tsx never renders it as
    a browsable list). Both are asserted here so this test stays true to the
    real UI regardless of which hook the panel is wired to next.
    """
    c, uid, persona_id, workspace_root = client
    conv_id = _new_conversation(c, uid, persona_id)
    _turn(c, uid, conv_id, "plan a weekly board game night")
    su = make_rls_engine(os.environ["DATABASE_URL"])
    try:
        message_id = _message_id(su, conv_id, role="assistant")
        resp = c.post(
            f"/v1/conversations/{conv_id}/messages/{message_id}/turn-into-file",
            json={"format": "auto"},
            headers=_auth(uid),
        )
        assert resp.status_code == 202, resp.text

        channel = _RecordingChannel()
        sandbox = _FakeCodeSandbox()
        _prune_other_jobs(su, uid)
        worker = _file_extract_worker(su, c.app.state.rls_engine, workspace_root, channel, sandbox)
        assert asyncio.run(worker.run_once()) == 1
        assert _file_extract_jobs(su, uid) == [(f"file_extract:{message_id}:auto", "succeeded")]

        # ----- surface 1: GET /v1/conversations/{id}/documents (leg B's target) --
        documents = c.get(f"/v1/conversations/{conv_id}/documents", headers=_auth(uid))
        assert documents.status_code == 200, documents.text
        docs = documents.json()
        assert len(docs) == 1
        doc = docs[0]
        assert doc["format"] == "pdf"
        assert doc["workspace_path"] == (
            f"{uid}/{persona_id}/conversations/{conv_id}/documents/{doc['doc_ref']}.pdf"
        )
        persisted_name = f"{doc['doc_ref']}.pdf"

        # A real file landed under the conversation documents dir (Spec 14's
        # own layout — see document_service's module docstring) with BOTH
        # sidecars: the DocumentRef .meta.json (what the GET above just read)
        # and the F5 .f5.json (surface 2, below).
        target = (
            workspace_root
            / uid
            / persona_id
            / "conversations"
            / conv_id
            / "documents"
            / persisted_name
        )
        assert target.read_bytes() == b"%PDF-fake"
        meta = read_artifact_sidecar(target)
        assert meta is not None
        assert meta.source == "generated"
        assert meta.type == "doc"
        assert meta.producing_spec == "16"
        assert meta.conversation_id == conv_id

        # The sandbox session was ISOLATED from the live chat conversation_id.
        (call,) = sandbox.execute_calls
        assert call["session_id"] == f"{uid}:file-extract-{message_id}"

        # ----- surface 2: GET /v1/personas/{id}/artifacts (what the web panel
        # ACTUALLY reads today — useConversationArtifacts) -----------------------
        listed = c.get(
            f"/v1/personas/{persona_id}/artifacts",
            params={"conversation_id": conv_id},
            headers=_auth(uid),
        )
        assert listed.status_code == 200, listed.text
        refs = [item["ref"] for item in listed.json()["items"]]
        assert f"conversations/{conv_id}/documents/{persisted_name}" in refs

        # The refresh signal fired — conversation-files.tsx now subscribes to it
        # live (R9-028 rider, eedf95d), on top of its refresh-on-open floor.
        file_pings = [
            (owner, ev)
            for owner, ev in channel.published
            if getattr(ev, "reason", None) == "conversation.file_extracted"
        ]
        assert len(file_pings) == 1
        owner, event = file_pings[0]
        assert owner == uid
        assert getattr(event, "type", None) == "sidebar.changed"
    finally:
        su.dispose()


# ----- idempotency: dedup at enqueue + convergence on redelivery --------------


def test_reenqueue_same_message_and_format_is_a_dedup_noop(
    client: tuple[TestClient, str, str, Path],
) -> None:
    c, uid, persona_id, _workspace_root = client
    conv_id = _new_conversation(c, uid, persona_id)
    _turn(c, uid, conv_id, "plan a weekly board game night")
    su = make_rls_engine(os.environ["DATABASE_URL"])
    try:
        message_id = _message_id(su, conv_id, role="assistant")
        first = c.post(
            f"/v1/conversations/{conv_id}/messages/{message_id}/turn-into-file",
            json={"format": "auto"},
            headers=_auth(uid),
        )
        assert first.status_code == 202
        assert first.json()["job_id"] is not None

        second = c.post(
            f"/v1/conversations/{conv_id}/messages/{message_id}/turn-into-file",
            json={"format": "auto"},
            headers=_auth(uid),
        )
        assert second.status_code == 202
        assert second.json()["job_id"] is None  # A0's ON CONFLICT dedup

        assert len(_file_extract_jobs(su, uid)) == 1
    finally:
        su.dispose()


def test_redelivery_does_not_crash_and_each_render_is_independently_listed(
    client: tuple[TestClient, str, str, Path],
) -> None:
    """A redelivery: same payload, a FRESH idempotency key (the synthesis /
    title_refresh redelivery-test pattern) — simulates an at-least-once
    re-run rather than a client re-click.

    R9-025 reopen leg B changed the persisted name from a bare, title-derived
    slug to ``slug-shorthash`` (matching ``document_service``'s own
    upload-doc_ref convention — see the module docstring) so a genuinely
    voice-less/collision-prone title never silently overwrites a DIFFERENT
    document. One side effect: two renders of "the same" extraction are no
    longer guaranteed to collide onto one physical file (uploads never did
    either — re-uploading the same-named file twice makes two DocumentRefs,
    not one). What matters for at-least-once safety is what's asserted here:
    neither render crashes/errors, and BOTH are independently valid, listed
    documents — never a corrupt half-write or an orphaned sidecar.
    """
    c, uid, persona_id, workspace_root = client
    conv_id = _new_conversation(c, uid, persona_id)
    _turn(c, uid, conv_id, "plan a weekly board game night")
    su = make_rls_engine(os.environ["DATABASE_URL"])
    try:
        message_id = _message_id(su, conv_id, role="assistant")
        c.post(
            f"/v1/conversations/{conv_id}/messages/{message_id}/turn-into-file",
            json={"format": "auto"},
            headers=_auth(uid),
        )
        channel = _RecordingChannel()
        sandbox = _FakeCodeSandbox()
        _prune_other_jobs(su, uid)
        worker = _file_extract_worker(su, c.app.state.rls_engine, workspace_root, channel, sandbox)
        assert asyncio.run(worker.run_once()) == 1

        # A redelivery: same payload, a FRESH idempotency key (the synthesis /
        # title_refresh redelivery-test pattern) — simulates an at-least-once
        # re-run rather than a client re-click.
        JobQueue(su).enqueue(
            type="file_extract",
            owner_id=uid,
            payload={"conversation_id": conv_id, "message_id": message_id, "format": "auto"},
            idempotency_key=f"file_extract:{message_id}:auto:redelivery",
        )
        assert asyncio.run(worker.run_once()) == 1
        assert _file_extract_jobs(su, uid) == [
            (f"file_extract:{message_id}:auto", "succeeded"),
            (f"file_extract:{message_id}:auto:redelivery", "succeeded"),
        ]

        documents = c.get(f"/v1/conversations/{conv_id}/documents", headers=_auth(uid))
        assert documents.status_code == 200, documents.text
        docs = documents.json()
        assert len(docs) == 2  # each render is its own document — never a crash/corruption
        assert len({d["doc_ref"] for d in docs}) == 2  # distinct refs, no accidental collision
        for d in docs:
            assert d["title"] == _EXTRACTED_TITLE
            assert d["format"] == "pdf"

        listed = c.get(
            f"/v1/personas/{persona_id}/artifacts",
            params={"conversation_id": conv_id},
            headers=_auth(uid),
        )
        assert listed.json()["total"] == 2  # both surfaces agree
    finally:
        su.dispose()


# ----- validation: wrong role 422, cross-tenant 404 ----------------------------


def test_user_message_target_422s(client: tuple[TestClient, str, str, Path]) -> None:
    c, uid, persona_id, _workspace_root = client
    conv_id = _new_conversation(c, uid, persona_id)
    _turn(c, uid, conv_id, "plan a weekly board game night")
    su = make_rls_engine(os.environ["DATABASE_URL"])
    try:
        user_message_id = _message_id(su, conv_id, role="user")
        resp = c.post(
            f"/v1/conversations/{conv_id}/messages/{user_message_id}/turn-into-file",
            json={"format": "auto"},
            headers=_auth(uid),
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["error"] == "wrong_message_role"
        assert _file_extract_jobs(su, uid) == []  # never enqueued
    finally:
        su.dispose()


def test_cross_tenant_conversation_404s(client: tuple[TestClient, str, str, Path]) -> None:
    c, uid, persona_id, _workspace_root = client
    conv_id = _new_conversation(c, uid, persona_id)
    _turn(c, uid, conv_id, "plan a weekly board game night")
    su = make_rls_engine(os.environ["DATABASE_URL"])
    try:
        message_id = _message_id(su, conv_id, role="assistant")
        other_uid = "user_r9025b_other"
        with su.begin() as conn:
            conn.execute(
                text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
                {"i": other_uid, "e": f"{other_uid}@x.test"},
            )
        try:
            resp = c.post(
                f"/v1/conversations/{conv_id}/messages/{message_id}/turn-into-file",
                json={"format": "auto"},
                headers=_auth(other_uid),
            )
            assert resp.status_code == 404, resp.text
            assert _file_extract_jobs(su, uid) == []  # never enqueued
        finally:
            with su.begin() as conn:
                conn.execute(text("DELETE FROM users WHERE id = :i"), {"i": other_uid})
    finally:
        su.dispose()
