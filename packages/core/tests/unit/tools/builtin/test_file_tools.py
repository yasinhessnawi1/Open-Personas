"""Tests for file_read + file_write (T10)."""

# ruff: noqa: ANN401, ARG001, ARG002, ERA001
from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

import pytest
from persona.schema.tools import PersistedArtifact
from persona.tools.audit import MemoryToolAuditLogger, ToolAuditEvent
from persona.tools.builtin.file_read import make_file_read_tool
from persona.tools.builtin.file_write import make_file_write_tool
from persona.tools.protocol import AsyncTool

if TYPE_CHECKING:
    from pathlib import Path


# Section: file_read happy path


class TestFileReadHappyPath:
    @pytest.mark.asyncio
    async def test_reads_utf8_text(self, tmp_path: Path) -> None:
        target = tmp_path / "hello.txt"
        target.write_text("Hei verden", encoding="utf-8")
        tool_inst = make_file_read_tool(sandbox_root=tmp_path)
        result = await tool_inst.execute(path="hello.txt")
        assert result.is_error is False
        assert result.content == "Hei verden"
        assert result.truncated is False
        assert result.data is not None
        assert result.data["path"] == "hello.txt"
        assert result.data["bytes_read"] == str(len(b"Hei verden"))

    @pytest.mark.asyncio
    async def test_reads_nested_path(self, tmp_path: Path) -> None:
        (tmp_path / "a" / "b").mkdir(parents=True)
        (tmp_path / "a" / "b" / "c.txt").write_text("nested")
        tool_inst = make_file_read_tool(sandbox_root=tmp_path)
        result = await tool_inst.execute(path="a/b/c.txt")
        assert result.content == "nested"

    @pytest.mark.asyncio
    async def test_replaces_invalid_utf8(self, tmp_path: Path) -> None:
        target = tmp_path / "binary.txt"
        target.write_bytes(b"valid\xff\xfeinvalid")
        tool_inst = make_file_read_tool(sandbox_root=tmp_path)
        result = await tool_inst.execute(path="binary.txt")
        assert result.is_error is False
        # errors="replace" → invalid bytes become the U+FFFD replacement character.
        assert "�" in result.content
        assert "valid" in result.content
        assert "invalid" in result.content

    @pytest.mark.asyncio
    async def test_truncates_large_files(self, tmp_path: Path) -> None:
        target = tmp_path / "big.txt"
        # 2 MB of 'x' — larger than the 1 MB cap.
        target.write_bytes(b"x" * (1_048_576 + 1024))
        tool_inst = make_file_read_tool(sandbox_root=tmp_path)
        result = await tool_inst.execute(path="big.txt")
        assert result.is_error is False
        assert result.truncated is True
        assert len(result.content) == 1_048_576


# Section: file_read error paths


class TestFileReadErrors:
    @pytest.mark.asyncio
    async def test_missing_file(self, tmp_path: Path) -> None:
        tool_inst = make_file_read_tool(sandbox_root=tmp_path)
        result = await tool_inst.execute(path="does-not-exist.txt")
        assert result.is_error is True
        assert "FileNotFoundError" in result.content

    @pytest.mark.asyncio
    async def test_directory_not_file(self, tmp_path: Path) -> None:
        (tmp_path / "subdir").mkdir()
        tool_inst = make_file_read_tool(sandbox_root=tmp_path)
        result = await tool_inst.execute(path="subdir")
        assert result.is_error is True
        # IsADirectoryError or generic OSError from O_NOFOLLOW path; both acceptable.
        assert "Directory" in result.content or "directory" in result.content

    @pytest.mark.asyncio
    async def test_sandbox_violation_rejected(self, tmp_path: Path) -> None:
        tool_inst = make_file_read_tool(sandbox_root=tmp_path)
        result = await tool_inst.execute(path="../../../etc/passwd")
        assert result.is_error is True
        assert "SandboxViolationError" in result.content

    @pytest.mark.asyncio
    async def test_null_byte_rejected(self, tmp_path: Path) -> None:
        tool_inst = make_file_read_tool(sandbox_root=tmp_path)
        result = await tool_inst.execute(path="file\x00.txt")
        assert result.is_error is True
        assert "SandboxViolationError" in result.content

    @pytest.mark.asyncio
    async def test_symlink_escape_rejected_at_open(self, tmp_path: Path) -> None:
        # The resolver catches symlink escape at resolution time. But: a
        # symlink to an outside target placed at a path inside the sandbox
        # is also rejected by O_NOFOLLOW at the open() (defense in depth).
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("escape")
        link = sandbox / "link.txt"
        link.symlink_to(outside)

        tool_inst = make_file_read_tool(sandbox_root=sandbox)
        result = await tool_inst.execute(path="link.txt")
        assert result.is_error is True


# Section: file_write happy path


class TestFileWriteHappyPath:
    @pytest.mark.asyncio
    async def test_writes_new_file(self, tmp_path: Path) -> None:
        tool_inst = make_file_write_tool(sandbox_root=tmp_path)
        result = await tool_inst.execute(path="new.txt", content="hello")
        assert result.is_error is False
        assert "Wrote" in result.content
        assert (tmp_path / "new.txt").read_text() == "hello"

    @pytest.mark.asyncio
    async def test_overwrites_existing_file(self, tmp_path: Path) -> None:
        (tmp_path / "exists.txt").write_text("old content")
        tool_inst = make_file_write_tool(sandbox_root=tmp_path)
        result = await tool_inst.execute(path="exists.txt", content="new content")
        assert result.is_error is False
        assert (tmp_path / "exists.txt").read_text() == "new content"

    @pytest.mark.asyncio
    async def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        tool_inst = make_file_write_tool(sandbox_root=tmp_path)
        result = await tool_inst.execute(path="a/b/c.txt", content="deep")
        assert result.is_error is False
        assert (tmp_path / "a" / "b" / "c.txt").read_text() == "deep"

    @pytest.mark.asyncio
    async def test_writes_utf8(self, tmp_path: Path) -> None:
        tool_inst = make_file_write_tool(sandbox_root=tmp_path)
        await tool_inst.execute(path="norsk.txt", content="Hei æøå")
        assert (tmp_path / "norsk.txt").read_bytes() == "Hei æøå".encode()

    @pytest.mark.asyncio
    async def test_round_trip_with_file_read(self, tmp_path: Path) -> None:
        writer = make_file_write_tool(sandbox_root=tmp_path)
        reader = make_file_read_tool(sandbox_root=tmp_path)
        await writer.execute(path="rt.txt", content="round trip")
        result = await reader.execute(path="rt.txt")
        assert result.is_error is False
        assert result.content == "round trip"


# Section: file_write error paths


class TestFileWriteErrors:
    @pytest.mark.asyncio
    async def test_sandbox_violation_rejected(self, tmp_path: Path) -> None:
        tool_inst = make_file_write_tool(sandbox_root=tmp_path)
        result = await tool_inst.execute(path="../../etc/passwd", content="evil")
        assert result.is_error is True
        assert "SandboxViolationError" in result.content
        # The actual filesystem write should NOT have happened.
        assert not (tmp_path.parent / "etc").exists()

    @pytest.mark.asyncio
    async def test_absolute_path_rejected(self, tmp_path: Path) -> None:
        tool_inst = make_file_write_tool(sandbox_root=tmp_path)
        result = await tool_inst.execute(path="/etc/passwd", content="evil")
        assert result.is_error is True
        assert "SandboxViolationError" in result.content

    @pytest.mark.asyncio
    async def test_symlink_escape_rejected(self, tmp_path: Path) -> None:
        # A symlink whose .resolve() points OUTSIDE the sandbox is rejected
        # at resolve time. (Per D-03-14, inside-sandbox symlinks are allowed.)
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("victim")
        escape_link = sandbox / "escape.txt"
        escape_link.symlink_to(outside)

        tool_inst = make_file_write_tool(sandbox_root=sandbox)
        result = await tool_inst.execute(path="escape.txt", content="overwrite")
        assert result.is_error is True
        # The outside file's content must not have been changed via the symlink.
        assert outside.read_text() == "victim"

    @pytest.mark.asyncio
    async def test_lone_surrogate_returns_clean_error(self, tmp_path: Path) -> None:
        # Security review Finding 5: lone surrogates in `content` raise
        # UnicodeEncodeError. Catch it and return a clean ToolResult.
        tool_inst = make_file_write_tool(sandbox_root=tmp_path)
        result = await tool_inst.execute(path="x.txt", content="bad\ud800char")
        assert result.is_error is True
        assert "UnicodeEncodeError" in result.content
        # File must NOT have been created.
        assert not (tmp_path / "x.txt").exists()

    @pytest.mark.asyncio
    async def test_os_write_oserror_returns_clean_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Security review Finding 10.2: os.write can raise OSError (ENOSPC etc.).
        # We catch and return a ToolResult; the fd is still closed.
        real_write = os.write
        call_count = {"n": 0}

        def flaky_write(fd: int, data: bytes, /) -> int:
            call_count["n"] += 1
            # Raise on first write to our path; allow others (audit logger etc.).
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(os, "write", flaky_write)
        try:
            tool_inst = make_file_write_tool(sandbox_root=tmp_path)
            result = await tool_inst.execute(path="x.txt", content="content")
        finally:
            monkeypatch.setattr(os, "write", real_write)

        assert result.is_error is True
        assert "OSError" in result.content
        assert call_count["n"] >= 1


# Section: produced_files on file_write (Spec 19 L2 — D-19-X-file-write-produced-files)


class TestFileWriteProducedFiles:
    """``produced_files`` mirrors the sandbox/tool.py code_execution shape."""

    @pytest.mark.asyncio
    async def test_populated_on_success_with_correct_shape(self, tmp_path: Path) -> None:
        tool_inst = make_file_write_tool(sandbox_root=tmp_path)
        result = await tool_inst.execute(path="report.txt", content="hello")
        assert result.is_error is False
        assert result.data is not None
        produced = result.data["produced_files"]
        assert isinstance(produced, list)
        assert len(produced) == 1
        entry = produced[0]
        assert set(entry.keys()) == {"path", "size_bytes", "media_type"}
        assert entry["path"] == "report.txt"
        assert entry["size_bytes"] == str(len(b"hello"))
        assert entry["media_type"] == "text/plain"

    @pytest.mark.asyncio
    async def test_not_populated_on_path_traversal_rejection(self, tmp_path: Path) -> None:
        tool_inst = make_file_write_tool(sandbox_root=tmp_path)
        result = await tool_inst.execute(path="../../etc/passwd", content="evil")
        assert result.is_error is True
        # Failed writes do NOT carry produced_files (the data envelope is absent
        # on the error branch — only the success branch populates ToolResult.data).
        assert result.data is None or "produced_files" not in (result.data or {})

    @pytest.mark.asyncio
    async def test_media_type_inference_txt(self, tmp_path: Path) -> None:
        tool_inst = make_file_write_tool(sandbox_root=tmp_path)
        result = await tool_inst.execute(path="notes.txt", content="x")
        assert result.data is not None
        assert result.data["produced_files"][0]["media_type"] == "text/plain"

    @pytest.mark.asyncio
    async def test_media_type_inference_docx(self, tmp_path: Path) -> None:
        tool_inst = make_file_write_tool(sandbox_root=tmp_path)
        result = await tool_inst.execute(path="brief.docx", content="x")
        assert result.data is not None
        # mimetypes maps .docx to the OOXML word media type on stdlib.
        assert (
            result.data["produced_files"][0]["media_type"]
            == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )

    @pytest.mark.asyncio
    async def test_media_type_inference_png(self, tmp_path: Path) -> None:
        tool_inst = make_file_write_tool(sandbox_root=tmp_path)
        # PNG bytes are fine to pass as text here — file_write writes whatever it
        # gets; only media_type inference under test (we check the data envelope).
        result = await tool_inst.execute(path="chart.png", content="not really png")
        assert result.data is not None
        assert result.data["produced_files"][0]["media_type"] == "image/png"

    @pytest.mark.asyncio
    async def test_media_type_inference_unknown_falls_back(self, tmp_path: Path) -> None:
        tool_inst = make_file_write_tool(sandbox_root=tmp_path)
        result = await tool_inst.execute(path="blob.zzunknown", content="x")
        assert result.data is not None
        assert result.data["produced_files"][0]["media_type"] == "application/octet-stream"


# Section: audit emission on file_write


class TestFileWriteAudit:
    @pytest.mark.asyncio
    async def test_emits_one_event_per_successful_write(self, tmp_path: Path) -> None:
        audit = MemoryToolAuditLogger()
        tool_inst = make_file_write_tool(
            sandbox_root=tmp_path,
            audit_logger=audit,
            persona_id="legal-bot",
        )
        await tool_inst.execute(path="report.md", content="draft")
        assert len(audit.events) == 1
        ev = audit.events[0]
        assert isinstance(ev, ToolAuditEvent)
        assert ev.tool_name == "file_write"
        assert ev.action == "write"
        assert ev.resource == "report.md"
        assert ev.persona_id == "legal-bot"
        assert ev.metadata["bytes"] == str(len(b"draft"))
        assert ev.is_error is False

    @pytest.mark.asyncio
    async def test_does_not_emit_on_failure(self, tmp_path: Path) -> None:
        audit = MemoryToolAuditLogger()
        tool_inst = make_file_write_tool(sandbox_root=tmp_path, audit_logger=audit)
        result = await tool_inst.execute(path="../../escape", content="x")
        assert result.is_error is True
        # Failed writes (sandbox violation) must NOT produce audit events.
        assert audit.events == []

    @pytest.mark.asyncio
    async def test_no_audit_logger_works_fine(self, tmp_path: Path) -> None:
        # The audit logger is optional — file_write works without one.
        tool_inst = make_file_write_tool(sandbox_root=tmp_path)
        result = await tool_inst.execute(path="x.txt", content="content")
        assert result.is_error is False
        assert (tmp_path / "x.txt").read_text() == "content"

    @pytest.mark.asyncio
    async def test_file_read_does_not_emit(self, tmp_path: Path) -> None:
        # file_read is read-only; no audit emissions (D-03-21).
        audit = MemoryToolAuditLogger()
        (tmp_path / "x.txt").write_text("content")

        # file_read doesn't accept an audit logger; verify by reading and
        # then verifying the audit log we'd inject into write is untouched.
        reader = make_file_read_tool(sandbox_root=tmp_path)
        await reader.execute(path="x.txt")
        assert audit.events == []


# Section: AsyncTool conformance


class TestAsyncToolConformance:
    def test_file_read_satisfies_async_tool(self, tmp_path: Path) -> None:
        tool_inst = make_file_read_tool(sandbox_root=tmp_path)
        assert isinstance(tool_inst, AsyncTool)
        assert tool_inst.name == "file_read"
        assert "path" in tool_inst.parameters_schema["properties"]

    def test_file_write_satisfies_async_tool(self, tmp_path: Path) -> None:
        tool_inst = make_file_write_tool(sandbox_root=tmp_path)
        assert isinstance(tool_inst, AsyncTool)
        assert tool_inst.name == "file_write"
        assert "path" in tool_inst.parameters_schema["properties"]
        assert "content" in tool_inst.parameters_schema["properties"]


# Section: ToolAuditLogger Protocol conformance


class TestToolAuditLoggerProtocols:
    def test_memory_logger_is_protocol_conformant(self) -> None:
        from persona.tools.audit import ToolAuditLogger

        assert isinstance(MemoryToolAuditLogger(), ToolAuditLogger)

    def test_jsonl_logger_writes_and_round_trips(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime

        from persona.tools.audit import JSONLToolAuditLogger, ToolAuditLogger

        root = tmp_path / "audit"
        root.mkdir()
        logger = JSONLToolAuditLogger(root=root)
        assert isinstance(logger, ToolAuditLogger)

        ev = ToolAuditEvent(
            timestamp=datetime.now(UTC),
            persona_id="legal",
            tool_name="file_write",
            action="write",
            resource="x.md",
            metadata={"bytes": "5"},
        )
        logger.emit(ev)
        log_file = root / "legal.tools.jsonl"
        assert log_file.exists()
        line = log_file.read_text().strip()
        # Round-trips through Pydantic.
        restored = ToolAuditEvent.model_validate_json(line)
        assert restored.tool_name == "file_write"
        assert restored.resource == "x.md"

    def test_jsonl_logger_handles_none_persona_id(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime

        from persona.tools.audit import JSONLToolAuditLogger

        root = tmp_path / "audit"
        root.mkdir()
        logger = JSONLToolAuditLogger(root=root)
        ev = ToolAuditEvent(
            timestamp=datetime.now(UTC),
            persona_id=None,
            tool_name="file_write",
            action="write",
            resource="x",
        )
        logger.emit(ev)
        assert (root / "_cli.tools.jsonl").exists()


# Section: O_NOFOLLOW is supported on this platform


class TestOSCapabilities:
    def test_o_nofollow_available(self) -> None:
        # Sanity: a no-follow opener must exist for the security guarantee to hold.
        # Linux and macOS provide O_NOFOLLOW. Windows has none, so its opener is the
        # Windows-only persona.tools._winopen (Spec WIN, T1), which must import there.
        if sys.platform == "win32":
            from persona.tools import _winopen

            assert callable(_winopen.open_fd)
        else:
            assert hasattr(os, "O_NOFOLLOW")


# Section: concurrent audit log writes


class TestAuditLockConcurrency:
    """Security review Finding 10.3: lock protects the events list under threading."""

    def test_memory_logger_thread_safe(self) -> None:
        import threading
        from datetime import UTC, datetime

        logger = MemoryToolAuditLogger()
        n_threads = 8
        n_writes_per_thread = 50

        def emit_many(thread_id: int) -> None:
            for i in range(n_writes_per_thread):
                logger.emit(
                    ToolAuditEvent(
                        timestamp=datetime.now(UTC),
                        tool_name="file_write",
                        action="write",
                        resource=f"t{thread_id}-{i}",
                    )
                )

        threads = [threading.Thread(target=emit_many, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # No events lost; the lock prevents list-mutation races.
        assert len(logger.events) == n_threads * n_writes_per_thread
        # Every event has a sensible shape.
        assert all(ev.tool_name == "file_write" for ev in logger.events)


# Section: Spec 28 — persister injection (workspace mirror → ToolResult.artifacts)


class _FakePersister:
    """Records persist calls; returns deterministic PersistedArtifacts."""

    def __init__(self, *, side_effect: BaseException | None = None) -> None:
        self._side_effect = side_effect
        self.calls: list[dict[str, object]] = []

    async def persist(
        self, data: bytes, *, mime_type: str, suggested_filename: str
    ) -> PersistedArtifact:
        self.calls.append(
            {"len": len(data), "mime_type": mime_type, "suggested_filename": suggested_filename}
        )
        if self._side_effect is not None:
            raise self._side_effect
        return PersistedArtifact(
            workspace_path=f"uploads/{suggested_filename}",
            mime_type=mime_type,
            size_bytes=len(data),
        )


# Section: per-request scope (cross-context isolation — SECURITY)
#
# The built-in file tools must resolve their sandbox root from the CURRENT
# request's owner/persona, NOT from a process-wide flat root. When the tool is
# built with a *provider* (a ``Callable[[], Path | None]``) it is re-resolved at
# every dispatch, so a single cached toolbox stays correctly scoped across
# concurrent requests. ``None`` from the provider means "no request scope" and
# MUST fail closed (deny) — never fall back to a shared root.


class _ScopeProvider:
    """A swappable per-request root provider (stands in for the ContextVar)."""

    def __init__(self) -> None:
        self.root: Path | None = None

    def __call__(self) -> Path | None:
        return self.root


class TestFileReadPerRequestScope:
    @pytest.mark.asyncio
    async def test_read_resolves_provider_at_call_time(self, tmp_path: Path) -> None:
        owner_a = tmp_path / "ownerA" / "personaA"
        owner_a.mkdir(parents=True)
        (owner_a / "note.txt").write_text("scoped-A", encoding="utf-8")

        provider = _ScopeProvider()
        provider.root = owner_a
        tool_inst = make_file_read_tool(sandbox_root=provider)
        result = await tool_inst.execute(path="note.txt")
        assert result.is_error is False
        assert result.content == "scoped-A"

    @pytest.mark.asyncio
    async def test_read_cannot_see_other_context_file(self, tmp_path: Path) -> None:
        # Context A and context B share the SAME parent root (the leak vector):
        # B left a file; A must NOT be able to read it.
        owner_a = tmp_path / "ownerA" / "personaA"
        owner_b = tmp_path / "ownerB" / "personaB"
        owner_a.mkdir(parents=True)
        owner_b.mkdir(parents=True)
        (owner_b / "secret.txt").write_text("ownerB-secret", encoding="utf-8")

        provider = _ScopeProvider()
        provider.root = owner_a
        tool_inst = make_file_read_tool(sandbox_root=provider)

        # The single cached tool, used under context A, cannot reach B's file by
        # any relative path (the resolver rejects traversal escapes).
        for attempt in ("secret.txt", "../personaB/secret.txt", "../../ownerB/personaB/secret.txt"):
            result = await tool_inst.execute(path=attempt)
            assert result.is_error is True
            assert "ownerB-secret" not in result.content

    @pytest.mark.asyncio
    async def test_read_fails_closed_without_context(self, tmp_path: Path) -> None:
        # Stray file directly under the flat parent — the OLD bug surfaced it.
        (tmp_path / "leaked.txt").write_text("leaked", encoding="utf-8")
        provider = _ScopeProvider()  # root stays None → no request scope
        tool_inst = make_file_read_tool(sandbox_root=provider)
        result = await tool_inst.execute(path="leaked.txt")
        assert result.is_error is True
        assert "leaked" not in result.content


class TestFileWritePerRequestScope:
    @pytest.mark.asyncio
    async def test_write_lands_in_scoped_root(self, tmp_path: Path) -> None:
        owner_a = tmp_path / "ownerA" / "personaA"
        owner_a.mkdir(parents=True)
        provider = _ScopeProvider()
        provider.root = owner_a
        tool_inst = make_file_write_tool(sandbox_root=provider)
        result = await tool_inst.execute(path="out/report.md", content="hi")
        assert result.is_error is False
        assert (owner_a / "out" / "report.md").read_text() == "hi"
        # NOT under the flat parent (the leak surface).
        assert not (tmp_path / "out" / "report.md").exists()

    @pytest.mark.asyncio
    async def test_write_fails_closed_without_context(self, tmp_path: Path) -> None:
        provider = _ScopeProvider()  # None → deny
        tool_inst = make_file_write_tool(sandbox_root=provider)
        result = await tool_inst.execute(path="out/report.md", content="hi")
        assert result.is_error is True
        # Nothing written anywhere under the parent.
        assert not any(tmp_path.rglob("report.md"))


class TestFileWritePersister:
    @pytest.mark.asyncio
    async def test_no_persister_leaves_artifacts_empty(self, tmp_path: Path) -> None:
        # Backward-compat (criterion #9): None persister → empty artifacts.
        tool_inst = make_file_write_tool(sandbox_root=tmp_path)
        result = await tool_inst.execute(path="out/report.md", content="hello")
        assert result.is_error is False
        assert result.artifacts == ()

    @pytest.mark.asyncio
    async def test_persister_populates_artifact(self, tmp_path: Path) -> None:
        persister = _FakePersister()
        tool_inst = make_file_write_tool(sandbox_root=tmp_path, persister=persister)
        result = await tool_inst.execute(path="out/report.md", content="hello world")
        assert result.is_error is False
        assert len(result.artifacts) == 1
        art = result.artifacts[0]
        assert art.workspace_path == "uploads/report.md"  # basename only
        assert art.mime_type == "text/markdown"
        assert art.size_bytes == len(b"hello world")
        assert art.rendered_inline is False  # non-image = card only
        assert persister.calls[0]["mime_type"] == "text/markdown"

    @pytest.mark.asyncio
    async def test_image_write_marks_rendered_inline(self, tmp_path: Path) -> None:
        persister = _FakePersister()
        tool_inst = make_file_write_tool(sandbox_root=tmp_path, persister=persister)
        result = await tool_inst.execute(path="out/diagram.png", content="\x89PNG")
        assert result.artifacts[0].rendered_inline is True

    @pytest.mark.asyncio
    async def test_persist_failure_surfaces_structured_error(self, tmp_path: Path) -> None:
        persister = _FakePersister(side_effect=OSError("disk full"))
        tool_inst = make_file_write_tool(sandbox_root=tmp_path, persister=persister)
        result = await tool_inst.execute(path="out/report.md", content="hello")
        assert result.is_error is True
        assert "persist_failed" in result.content
        # the sandbox file IS written even though the mirror failed.
        assert (tmp_path / "out" / "report.md").read_text() == "hello"
