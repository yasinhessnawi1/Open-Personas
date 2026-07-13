"""R9-025b — unit tests for :class:`SandboxFileRenderer`, the doc-gen sandbox boundary.

Drives a REAL :class:`persona_api.sandbox.pool.SandboxPool` (cheap, in-memory
bookkeeping — no external dependency) wrapped around a SCRIPTED fake
:class:`~persona.sandbox.protocol.CodeSandbox` — the boundary the R9-025b
kickoff calls out to script rather than hitting a real E2B/Docker substrate.
Verifies: bytes land on disk at the conversation documents path (R9-025 reopen
leg B) with BOTH the DocumentRef ``.meta.json`` sidecar (the
``GET /v1/conversations/{id}/documents`` shape) and the F5 ``.f5.json``
sidecar (per-format producing_spec/type mapping — the ``GET
/v1/personas/{id}/artifacts`` shape the chat Files viewer actually reads);
failure modes each raise :class:`FileExtractionError` with the documented
fixed-vocabulary reason; the sandbox session is ALWAYS released; and the
session is ISOLATED from the live chat conversation's own session id.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from persona.documents.ingest import IngestStrategy
from persona.errors import FileExtractionError
from persona.sandbox.errors import SandboxUnavailableError
from persona.sandbox.result import ExecutionResult, NetworkPolicy, ResourceLimits, SandboxFile
from persona_api.jobs.handlers.file_extract import ExtractedContent, SandboxFileRenderer
from persona_api.sandbox.pool import SandboxPool
from persona_api.services.artifact_metadata import read_artifact_sidecar
from persona_api.services.document_service import DocumentRef

pytestmark = pytest.mark.asyncio


def _document_ref_sidecar(target: Path) -> DocumentRef:
    """Read the DocumentRef ``.meta.json`` sidecar next to ``target`` (bytes path)."""
    sidecar = target.with_name(target.name + ".meta.json")
    return DocumentRef.model_validate_json(sidecar.read_text(encoding="utf-8"))


class _FakeCodeSandbox:
    """Scripted CodeSandbox: configurable outcome/produced-files/failures.

    ``copy_produced_file_to`` WRITES real bytes (unlike the runtime_tool.py
    test fakes, which only record the call) — SandboxFileRenderer's contract
    is "bytes really land on disk," so the fake must actually produce them.
    """

    def __init__(
        self,
        *,
        outcome: str = "ok",
        produced_files: tuple[SandboxFile, ...] = (),
        produced_bytes: dict[str, bytes] | None = None,
        raise_on_execute: Exception | None = None,
        raise_on_copy: Exception | None = None,
    ) -> None:
        self.outcome = outcome
        self.produced_files = produced_files
        self.produced_bytes = produced_bytes or {}
        self._raise_on_execute = raise_on_execute
        self._raise_on_copy = raise_on_copy
        self.execute_calls: list[dict[str, object]] = []
        self.copy_calls: list[dict[str, object]] = []
        self.created_sessions: set[str] = set()
        self.destroyed_sessions: set[str] = set()

    async def execute(
        self,
        code: str,
        *,
        language: str = "python",  # noqa: ARG002 — Protocol contract; fake doesn't use it
        session_id: str | None = None,
        timeout_s: float = 30.0,  # noqa: ARG002 — Protocol contract; fake doesn't use it
        limits: ResourceLimits | None = None,  # noqa: ARG002 — Protocol contract
        network: NetworkPolicy | None = None,  # noqa: ARG002 — Protocol contract
        input_files: list[SandboxFile] | None = None,  # noqa: ARG002 — Protocol contract
    ) -> ExecutionResult:
        self.execute_calls.append({"code": code, "session_id": session_id})
        if self._raise_on_execute is not None:
            raise self._raise_on_execute
        return ExecutionResult(
            stdout="",
            stderr="",
            exit_status=0 if self.outcome == "ok" else 1,
            outcome=self.outcome,  # type: ignore[arg-type]
            produced_files=self.produced_files,
        )

    async def create_session(
        self,
        session_id: str,
        *,
        limits: ResourceLimits,  # noqa: ARG002 — Protocol contract; fake doesn't use it
        network: NetworkPolicy,  # noqa: ARG002 — Protocol contract; fake doesn't use it
    ) -> None:
        self.created_sessions.add(session_id)

    async def destroy_session(self, session_id: str) -> None:
        self.destroyed_sessions.add(session_id)

    async def aclose(self) -> None:
        pass

    async def copy_produced_file_to(self, session_id: str, ref: str, target_path: Path) -> None:
        self.copy_calls.append({"session_id": session_id, "ref": ref, "target_path": target_path})
        if self._raise_on_copy is not None:
            raise self._raise_on_copy
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(self.produced_bytes.get(ref, b""))

    async def read_produced_file_bytes(self, session_id: str, ref: str) -> bytes:  # noqa: ARG002
        return self.produced_bytes.get(ref, b"")


def _content(*, title: str = "Board Game Report", kind: str = "prose") -> ExtractedContent:
    return ExtractedContent(title=title, kind=kind, body_markdown="Weekly, rotating hosts.")  # type: ignore[arg-type]


async def test_success_persists_bytes_writes_both_sidecars(tmp_path: Path) -> None:
    fake = _FakeCodeSandbox(
        produced_files=(
            SandboxFile(path="board-game-report.pdf", size_bytes=9, media_type="application/pdf"),
        ),
        produced_bytes={"board-game-report.pdf": b"%PDF-fake"},
    )
    pool = SandboxPool(sandbox=fake)
    renderer = SandboxFileRenderer(pool=pool, workspace_root=tmp_path)

    result = await renderer.render(
        _content(),
        format="pdf",
        owner_id="owner_1",
        persona_id="persona_1",
        conversation_id="conv_1",
        message_id="msg_1",
    )

    # Lands under the SAME conversation documents dir Spec 14 uploads use — the
    # surface GET /v1/conversations/{id}/documents reads — named
    # slug-shorthash (never the bare, collision-prone slug).
    assert result.workspace_path.startswith("conversations/conv_1/documents/board-game-report-")
    assert result.workspace_path.endswith(".pdf")
    assert result.media_type == "application/pdf"
    assert result.size_bytes == 9
    assert result.format == "pdf"

    persisted_name = result.workspace_path.rsplit("/", 1)[-1]
    target = (
        tmp_path
        / "owner_1"
        / "persona_1"
        / "conversations"
        / "conv_1"
        / "documents"
        / persisted_name
    )
    assert target.read_bytes() == b"%PDF-fake"

    # DocumentRef .meta.json — the shape the documents-list route reads.
    doc = _document_ref_sidecar(target)
    assert f"{doc.doc_ref}.pdf" == persisted_name
    assert doc.filename == persisted_name
    assert doc.title == "Board Game Report"
    assert doc.format == "pdf"
    assert doc.strategy == IngestStrategy.RETRIEVAL
    assert doc.size_bytes == 9
    assert doc.token_count > 0
    assert doc.workspace_path == (
        f"owner_1/persona_1/conversations/conv_1/documents/{persisted_name}"
    )

    # F5 .f5.json — UNCHANGED contract, new location; the shape the chat Files
    # viewer (GET /v1/personas/{id}/artifacts, useConversationArtifacts) reads.
    meta = read_artifact_sidecar(target)
    assert meta is not None
    assert meta.source == "generated"
    assert meta.type == "doc"
    assert meta.producing_spec == "16"
    assert meta.conversation_id == "conv_1"
    # Human-readable (with extension), not the on-disk slug — a display-name
    # improvement the panel's nameOf() picks up directly.
    assert meta.original_name == "Board Game Report.pdf"


@pytest.mark.parametrize(
    ("format_", "expected_producing_spec", "expected_type"),
    [
        ("pdf", "16", "doc"),
        ("xlsx", "16", "doc"),
        ("csv", "12", "data"),
        ("md", "12", "doc"),
    ],
)
async def test_producing_spec_and_sidecar_type_per_format(
    tmp_path: Path, format_: str, expected_producing_spec: str, expected_type: str
) -> None:
    ext = {"pdf": ".pdf", "xlsx": ".xlsx", "csv": ".csv", "md": ".md"}[format_]
    ref = f"board-game-report{ext}"
    fake = _FakeCodeSandbox(
        produced_files=(
            SandboxFile(path=ref, size_bytes=3, media_type="application/octet-stream"),
        ),
        produced_bytes={ref: b"abc"},
    )
    pool = SandboxPool(sandbox=fake)
    renderer = SandboxFileRenderer(pool=pool, workspace_root=tmp_path)

    result = await renderer.render(
        _content(),
        format=format_,
        owner_id="owner_1",
        persona_id="persona_1",
        conversation_id="conv_1",
        message_id="msg_1",
    )

    persisted_name = result.workspace_path.rsplit("/", 1)[-1]
    target = (
        tmp_path
        / "owner_1"
        / "persona_1"
        / "conversations"
        / "conv_1"
        / "documents"
        / persisted_name
    )
    meta = read_artifact_sidecar(target)
    assert meta is not None
    assert meta.producing_spec == expected_producing_spec
    assert meta.type == expected_type


async def test_session_is_isolated_from_the_live_chat_conversation(tmp_path: Path) -> None:
    fake = _FakeCodeSandbox(
        produced_files=(
            SandboxFile(path="board-game-report.pdf", size_bytes=1, media_type="application/pdf"),
        ),
        produced_bytes={"board-game-report.pdf": b"x"},
    )
    pool = SandboxPool(sandbox=fake)
    renderer = SandboxFileRenderer(pool=pool, workspace_root=tmp_path)

    await renderer.render(
        _content(),
        format="pdf",
        owner_id="owner_1",
        # The LIVE chat conversation id — the renderer must NOT reuse it as
        # its sandbox session scope (would share interpreter state with an
        # in-progress interactive turn).
        conversation_id="conv_live_chat",
        persona_id="persona_1",
        message_id="msg_1",
    )

    (call,) = fake.execute_calls
    session_id = call["session_id"]
    assert session_id == "owner_1:file-extract-msg_1"
    assert session_id != "owner_1:conv_live_chat"


async def test_session_is_released_after_a_successful_render(tmp_path: Path) -> None:
    fake = _FakeCodeSandbox(
        produced_files=(
            SandboxFile(path="board-game-report.pdf", size_bytes=1, media_type="application/pdf"),
        ),
        produced_bytes={"board-game-report.pdf": b"x"},
    )
    pool = SandboxPool(sandbox=fake)
    renderer = SandboxFileRenderer(pool=pool, workspace_root=tmp_path)
    await renderer.render(
        _content(),
        format="pdf",
        owner_id="owner_1",
        persona_id="persona_1",
        conversation_id="conv_1",
        message_id="msg_1",
    )
    assert fake.destroyed_sessions == {"owner_1:file-extract-msg_1"}


async def test_execute_dispatch_failure_raises_sandbox_execution_failed(tmp_path: Path) -> None:
    fake = _FakeCodeSandbox(raise_on_execute=SandboxUnavailableError("docker down"))
    pool = SandboxPool(sandbox=fake)
    renderer = SandboxFileRenderer(pool=pool, workspace_root=tmp_path)

    with pytest.raises(FileExtractionError) as excinfo:
        await renderer.render(
            _content(),
            format="pdf",
            owner_id="owner_1",
            persona_id="persona_1",
            conversation_id="conv_1",
            message_id="msg_1",
        )
    assert excinfo.value.context.get("reason") == "sandbox_execution_failed"
    # The session is still released even though dispatch failed.
    assert fake.destroyed_sessions == {"owner_1:file-extract-msg_1"}


async def test_non_ok_outcome_raises_sandbox_execution_failed(tmp_path: Path) -> None:
    fake = _FakeCodeSandbox(outcome="error")
    pool = SandboxPool(sandbox=fake)
    renderer = SandboxFileRenderer(pool=pool, workspace_root=tmp_path)

    with pytest.raises(FileExtractionError) as excinfo:
        await renderer.render(
            _content(),
            format="pdf",
            owner_id="owner_1",
            persona_id="persona_1",
            conversation_id="conv_1",
            message_id="msg_1",
        )
    assert excinfo.value.context.get("reason") == "sandbox_execution_failed"
    assert excinfo.value.context.get("outcome") == "error"


async def test_missing_produced_file_raises_empty_output(tmp_path: Path) -> None:
    fake = _FakeCodeSandbox(produced_files=())  # code ran ok but wrote nothing usable
    pool = SandboxPool(sandbox=fake)
    renderer = SandboxFileRenderer(pool=pool, workspace_root=tmp_path)

    with pytest.raises(FileExtractionError) as excinfo:
        await renderer.render(
            _content(),
            format="pdf",
            owner_id="owner_1",
            persona_id="persona_1",
            conversation_id="conv_1",
            message_id="msg_1",
        )
    assert excinfo.value.context.get("reason") == "empty_output"
    assert fake.destroyed_sessions == {"owner_1:file-extract-msg_1"}


async def test_zero_byte_produced_file_raises_empty_output(tmp_path: Path) -> None:
    fake = _FakeCodeSandbox(
        produced_files=(
            SandboxFile(path="board-game-report.pdf", size_bytes=0, media_type="application/pdf"),
        ),
    )
    pool = SandboxPool(sandbox=fake)
    renderer = SandboxFileRenderer(pool=pool, workspace_root=tmp_path)

    with pytest.raises(FileExtractionError) as excinfo:
        await renderer.render(
            _content(),
            format="pdf",
            owner_id="owner_1",
            persona_id="persona_1",
            conversation_id="conv_1",
            message_id="msg_1",
        )
    assert excinfo.value.context.get("reason") == "empty_output"


async def test_copy_failure_raises_sandbox_execution_failed_and_releases(tmp_path: Path) -> None:
    fake = _FakeCodeSandbox(
        produced_files=(
            SandboxFile(path="board-game-report.pdf", size_bytes=1, media_type="application/pdf"),
        ),
        raise_on_copy=SandboxUnavailableError("substrate vanished mid-copy"),
    )
    pool = SandboxPool(sandbox=fake)
    renderer = SandboxFileRenderer(pool=pool, workspace_root=tmp_path)

    with pytest.raises(FileExtractionError) as excinfo:
        await renderer.render(
            _content(),
            format="pdf",
            owner_id="owner_1",
            persona_id="persona_1",
            conversation_id="conv_1",
            message_id="msg_1",
        )
    assert excinfo.value.context.get("reason") == "sandbox_execution_failed"
    assert fake.destroyed_sessions == {"owner_1:file-extract-msg_1"}


async def test_unsupported_format_raises_before_touching_the_pool(tmp_path: Path) -> None:
    fake = _FakeCodeSandbox()
    pool = SandboxPool(sandbox=fake)
    renderer = SandboxFileRenderer(pool=pool, workspace_root=tmp_path)

    with pytest.raises(ValueError, match="unsupported render format"):
        await renderer.render(
            _content(),
            format="docx",
            owner_id="owner_1",
            persona_id="persona_1",
            conversation_id="conv_1",
            message_id="msg_1",
        )
    assert fake.execute_calls == []
    assert fake.created_sessions == set()
