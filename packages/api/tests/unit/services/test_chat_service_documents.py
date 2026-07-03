"""Document-workspace cascade — chat_service document resolution + forwarding.

Uploaded NON-image documents reached the model only as a ``document_context``
synopsis; the sandbox ``file_read`` / ``code_execution`` tools never saw the
actual file (so ``file_read`` could surface a stale, unrelated README from
another context). Two contracts close that gap:

* ``_resolve_turn_documents`` reads each attached document's ORIGINAL bytes from
  the conversation's documents directory and builds :class:`SandboxFile`
  carriers staged at ``uploads/<filename>``.
* ``stream_chat`` forwards the resolved documents to ``loop.turn(documents=...)``
  so the loop stages them onto ``deferred_input_files`` and the sandbox tools
  read THIS document.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from persona.documents.ingest import IngestStrategy
from persona.schema.conversation import Conversation
from persona_api.services import chat_service
from persona_api.services.chat_service import _resolve_turn_documents
from persona_api.services.document_service import DOCUMENT_DIR_NAME, DocumentRef
from persona_api.storage import LocalFileStorage

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from persona.backends import StreamChunk
    from persona_runtime.agentic.events import RunEvent

_PERSONA = "astrid"
_CONV = "c1"
_OWNER = "owner_1"
_DOC_BODY = b"# Polly's Layout Test\n\nThis is the ACTUAL uploaded document.\n"


def _write_document(
    workspace_root: Path,
    *,
    owner: str = _OWNER,
    doc_ref: str = "pollys_layout_test",
    filename: str = "pollys-layout-test.md",
    body: bytes = _DOC_BODY,
) -> DocumentRef:
    """Lay down an attached document the way document_service.upload does."""
    base = workspace_root / owner / _PERSONA / "conversations" / _CONV / DOCUMENT_DIR_NAME
    base.mkdir(parents=True, exist_ok=True)
    original = base / f"{doc_ref}.md"
    original.write_bytes(body)
    relative_path = f"{owner}/{_PERSONA}/conversations/{_CONV}/{DOCUMENT_DIR_NAME}/{doc_ref}.md"
    ref = DocumentRef(
        doc_ref=doc_ref,
        filename=filename,
        title=filename,
        format="md",
        workspace_path=relative_path,
        strategy=IngestStrategy.WHOLE_INJECT,
        token_count=10,
        size_bytes=len(body),
    )
    sidecar = original.with_suffix(original.suffix + ".meta.json")
    sidecar.write_text(ref.model_dump_json())
    return ref


class TestResolveTurnDocuments:
    def test_resolves_original_bytes_at_uploads_path(self, tmp_path: Path) -> None:
        _write_document(tmp_path)

        staged = _resolve_turn_documents(
            file_storage=LocalFileStorage(tmp_path),
            owner_id=_OWNER,
            persona_id=_PERSONA,
            conversation_id=_CONV,
        )

        assert len(staged) == 1
        sf = staged[0]
        assert sf.path == "uploads/pollys-layout-test.md"
        assert sf.content_bytes == _DOC_BODY
        assert sf.media_type == "text/markdown"

    def test_no_workspace_root_returns_empty(self) -> None:
        assert (
            _resolve_turn_documents(
                file_storage=None, owner_id=_OWNER, persona_id=_PERSONA, conversation_id=_CONV
            )
            == []
        )

    def test_no_documents_returns_empty(self, tmp_path: Path) -> None:
        assert (
            _resolve_turn_documents(
                file_storage=LocalFileStorage(tmp_path),
                owner_id=_OWNER,
                persona_id=_PERSONA,
                conversation_id=_CONV,
            )
            == []
        )

    def test_oversize_document_is_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_document(tmp_path, body=b"x" * 1024)
        monkeypatch.setattr(chat_service, "MAX_STAGED_DOCUMENT_BYTES", 10)

        assert (
            _resolve_turn_documents(
                file_storage=LocalFileStorage(tmp_path),
                owner_id=_OWNER,
                persona_id=_PERSONA,
                conversation_id=_CONV,
            )
            == []
        )


class TestStageDocumentsForFileRead:
    """The host-side mirror that makes ``file_read("uploads/<name>")`` work.

    ``code_execution`` reads the staged document because the runtime ships the
    bytes into the REMOTE sandbox CWD; ``file_read`` reads the LOCAL filesystem
    under ``<workspace_root>/<owner_id>/<persona_id>``. The mirror writes the
    document under THAT scoped root so the SAME relative path serves both tools.
    """

    _OWNER = "owner_1"

    def _resolved(self, tmp_path: Path) -> list[object]:
        _write_document(tmp_path)
        return list(
            _resolve_turn_documents(
                file_storage=LocalFileStorage(tmp_path),
                owner_id=_OWNER,
                persona_id=_PERSONA,
                conversation_id=_CONV,
            )
        )

    def test_mirrors_document_into_file_read_scoped_root(self, tmp_path: Path) -> None:
        from persona_api.services.chat_service import _stage_documents_for_file_read

        documents = self._resolved(tmp_path)
        _stage_documents_for_file_read(
            workspace_root=tmp_path,
            owner_id=self._OWNER,
            persona_id=_PERSONA,
            documents=documents,  # type: ignore[arg-type]
        )

        # file_read resolves <workspace_root>/<owner_id>/<persona_id>/<path>;
        # the mirrored doc must land at uploads/<filename> under THAT root.
        target = tmp_path / self._OWNER / _PERSONA / "uploads" / "pollys-layout-test.md"
        assert target.exists()
        assert target.read_bytes() == _DOC_BODY

    def test_no_workspace_root_is_noop(self) -> None:
        from persona_api.services.chat_service import _stage_documents_for_file_read

        # Must not raise even with documents present (CLI / test path).
        _stage_documents_for_file_read(
            workspace_root=None, owner_id=self._OWNER, persona_id=_PERSONA, documents=[]
        )

    def test_isolation_persona_a_cannot_reach_persona_b(self, tmp_path: Path) -> None:
        """A document's mirror lands ONLY under the current owner/persona root.

        Persona B's scoped root must stay empty — the invariant the just-landed
        security scoping guarantees, preserved by the mirror.
        """
        from persona_api.services.chat_service import _stage_documents_for_file_read

        documents = self._resolved(tmp_path)
        _stage_documents_for_file_read(
            workspace_root=tmp_path,
            owner_id=self._OWNER,
            persona_id=_PERSONA,
            documents=documents,  # type: ignore[arg-type]
        )

        # Persona B (different persona, same owner) gets NOTHING.
        other_persona_root = tmp_path / self._OWNER / "other_persona"
        assert not other_persona_root.exists()
        # A different owner gets NOTHING either.
        other_owner_root = tmp_path / "owner_2"
        assert not other_owner_root.exists()

    def test_traversal_filename_does_not_escape_scoped_root(self, tmp_path: Path) -> None:
        """A pathological ``SandboxFile.path`` is rejected, not written outside."""
        from persona.sandbox.result import SandboxFile
        from persona_api.services.chat_service import _stage_documents_for_file_read

        evil = SandboxFile(
            path="uploads/../../escape.md",
            content_bytes=b"nope",
            size_bytes=4,
            media_type="text/markdown",
        )
        _stage_documents_for_file_read(
            workspace_root=tmp_path,
            owner_id=self._OWNER,
            persona_id=_PERSONA,
            documents=[evil],
        )
        # Nothing escaped above the scoped root.
        assert not (tmp_path / "escape.md").exists()
        assert not (tmp_path / self._OWNER / "escape.md").exists()


class TestFileReadReconciliation:
    """End-to-end: a mirrored document is readable by the REAL ``file_read`` tool.

    Proves the path model is coherent — ``file_read("uploads/<filename>")``
    resolves against the SAME per-(owner, persona) scoped root the mirror writes
    to, and reads THIS document's actual bytes. Also proves the isolation
    invariant against the real tool: persona B's file_read (its own scoped root)
    cannot see persona A's mirrored document.
    """

    _OWNER = "owner_1"

    @pytest.mark.asyncio
    async def test_file_read_reads_mirrored_document(self, tmp_path: Path) -> None:
        from persona.tools.builtin.file_read import make_file_read_tool
        from persona_api.services.chat_service import _stage_documents_for_file_read

        _write_document(tmp_path)
        documents = list(
            _resolve_turn_documents(
                file_storage=LocalFileStorage(tmp_path),
                owner_id=_OWNER,
                persona_id=_PERSONA,
                conversation_id=_CONV,
            )
        )
        _stage_documents_for_file_read(
            workspace_root=tmp_path,
            owner_id=self._OWNER,
            persona_id=_PERSONA,
            documents=documents,  # type: ignore[arg-type]
        )

        # The file_read provider resolves <workspace_root>/<owner>/<persona>
        # (mirror of runtime_factory._build_file_sandbox_root_provider).
        scoped_root = tmp_path / self._OWNER / _PERSONA
        tool = make_file_read_tool(sandbox_root=scoped_root)
        result = await tool.execute(path="uploads/pollys-layout-test.md")

        assert result.is_error is False
        assert result.content == _DOC_BODY.decode()

    @pytest.mark.asyncio
    async def test_file_read_isolation_other_persona_cannot_read(self, tmp_path: Path) -> None:
        from persona.tools.builtin.file_read import make_file_read_tool
        from persona_api.services.chat_service import _stage_documents_for_file_read

        _write_document(tmp_path)
        documents = list(
            _resolve_turn_documents(
                file_storage=LocalFileStorage(tmp_path),
                owner_id=_OWNER,
                persona_id=_PERSONA,
                conversation_id=_CONV,
            )
        )
        _stage_documents_for_file_read(
            workspace_root=tmp_path,
            owner_id=self._OWNER,
            persona_id=_PERSONA,
            documents=documents,  # type: ignore[arg-type]
        )

        # Persona B's file_read is scoped to ITS OWN root — A's doc is invisible.
        other_root = tmp_path / self._OWNER / "persona_b"
        other_root.mkdir(parents=True, exist_ok=True)
        tool = make_file_read_tool(sandbox_root=other_root)
        result = await tool.execute(path="uploads/pollys-layout-test.md")

        assert result.is_error is True
        assert "FileNotFoundError" in result.content


class _CapturingLoop:
    """Scripted loop that records the ``documents`` kwarg it received."""

    def __init__(self) -> None:
        self.documents_seen: list[object] | None = None

    async def turn(
        self,
        conversation: Conversation,
        user_message: str,
        on_event: Callable[[RunEvent], Awaitable[None]] | None = None,  # noqa: ARG002
        *,
        turn_has_image: bool = False,  # noqa: ARG002
        images: list[object] | None = None,  # noqa: ARG002
        documents: list[object] | None = None,
        document_context: object = None,  # noqa: ARG002
    ) -> AsyncIterator[StreamChunk]:
        from persona.backends import StreamChunk
        from persona.schema.conversation import ConversationMessage

        self.documents_seen = documents
        now = datetime.now(UTC)
        conversation.messages.append(
            ConversationMessage(role="user", content=user_message, created_at=now)
        )
        conversation.messages.append(
            ConversationMessage(role="assistant", content="ok", created_at=now)
        )
        yield StreamChunk(delta="ok", is_final=False)
        yield StreamChunk(delta="", is_final=True)


class _FakeSink:
    """A Postgres-free ChatTurnSink: open_turn returns an id; the rest no-op."""

    def open_turn(self, **_kwargs: object) -> str:
        return "msg_assistant"

    def checkpoint(self, **_kwargs: object) -> None: ...

    def finalize(self, **_kwargs: object) -> None: ...


class _FakeConn:
    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *_a: object) -> None: ...


class _FakeEngine:
    def begin(self) -> _FakeConn:
        return _FakeConn()


@pytest.mark.asyncio
async def test_start_chat_turn_forwards_resolved_documents_to_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from persona_api.background.chat_turn_worker import ChatTurnRegistry

    _write_document(tmp_path)
    captured = _CapturingLoop()

    monkeypatch.setattr(
        chat_service,
        "_load_conversation",
        lambda _conn, _cid: Conversation(conversation_id=_CONV, persona_id=_PERSONA, messages=[]),
    )

    async def _build_loop(_pid: str) -> _CapturingLoop:
        return captured

    handle = await chat_service.start_chat_turn(
        rls_engine=_FakeEngine(),  # type: ignore[arg-type]
        sink=_FakeSink(),  # type: ignore[arg-type]
        registry=ChatTurnRegistry(sink=_FakeSink()),  # type: ignore[arg-type]
        loop_builder=_build_loop,  # type: ignore[arg-type]
        owner_id=_OWNER,
        conversation_id=_CONV,
        user_message="summarise the doc",
        channel=None,
        workspace_root=tmp_path,
        file_storage=LocalFileStorage(tmp_path),
    )
    await handle.task

    assert captured.documents_seen is not None
    assert len(captured.documents_seen) == 1
    sf = captured.documents_seen[0]
    assert sf.path == "uploads/pollys-layout-test.md"  # type: ignore[attr-defined]
    assert sf.content_bytes == _DOC_BODY  # type: ignore[attr-defined]
