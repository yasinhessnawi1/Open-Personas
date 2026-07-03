"""Spec 28 D-integration — the rich-output chain, end-to-end with the REAL adapter.

Glues the concrete :class:`WorkspaceDirPersister` (not a fake) to the real
byte-producing tools and the SSE event forwarder, proving the full chain for
every Spec 28 surface:

    tool dispatch → WorkspaceDirPersister.persist (bytes on disk + .f5.json
    sidecar, producing_spec="28") → ToolResult.artifacts → RunEvent.tool_result
    forwards artifacts onto the payload (chat + run transports).

The web half of the chain (payload → file-card OutputContent) is covered by the
vitest normaliser tests (`packages/web/src/lib/normalisers/artifacts.test.ts`).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from persona.imagegen import make_generate_image_tool
from persona.imagegen.result import GeneratedImage, GenerationResult
from persona.tools.builtin.file_write import make_file_write_tool
from persona.tools.builtin.render_diagram import make_render_diagram_tool
from persona_api.sandbox import (
    SandboxRequestContext,
    reset_sandbox_request_context,
    set_sandbox_request_context,
)
from persona_api.services.workspace_persister import WorkspaceDirPersister
from persona_api.storage import LocalFileStorage
from persona_runtime.agentic.events import RunEvent

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from persona.imagegen.result import ImageGenOptions

pytestmark = pytest.mark.integration


class _FakeImageBackend:
    provider_name = "fake"
    model_name = "fake-1"

    async def generate(
        self, prompt: str, *, options: ImageGenOptions | None = None
    ) -> GenerationResult:
        _ = (prompt, options)  # structural fake; args unused
        return GenerationResult(
            images=[
                GeneratedImage(
                    image_bytes=b"\x89PNG\r\n\x1a\n",
                    media_type="image/png",
                    width=512,
                    height=512,
                )
            ],
            provider=self.provider_name,
            model=self.model_name,
            latency_ms=1.0,
        )

    async def edit(self, *a: object, **k: object) -> GenerationResult:  # noqa: ARG002
        raise NotImplementedError


@pytest.fixture
def ctx() -> Iterator[None]:
    token = set_sandbox_request_context(
        SandboxRequestContext(owner_id="user-1", conversation_id="conv-1")
    )
    try:
        yield
    finally:
        reset_sandbox_request_context(token)


def _persisted_path(workspace_root: Path, ref: str) -> Path:
    return workspace_root / "user-1" / "astrid" / ref


@pytest.mark.asyncio
@pytest.mark.usefixtures("ctx")
async def test_file_write_chain(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    persister = WorkspaceDirPersister(file_storage=LocalFileStorage(workspace), persona_id="astrid")
    tool = make_file_write_tool(sandbox_root=sandbox, persister=persister)

    result = await tool.execute(path="out/report.md", content="# Title\n")

    assert len(result.artifacts) == 1
    art = result.artifacts[0]
    assert art.mime_type == "text/markdown"
    # bytes on disk + F5 sidecar with producing_spec="28"
    on_disk = _persisted_path(workspace, art.workspace_path)
    assert on_disk.read_bytes() == b"# Title\n"
    sidecar = on_disk.with_name(on_disk.name + ".f5.json")
    assert json.loads(sidecar.read_text())["producing_spec"] == "28"
    # event forwarding (chat + run share this constructor)
    event = RunEvent.tool_result(step=0, tool_name="file_write", result=result)
    assert event.data["artifacts"][0]["workspace_path"] == art.workspace_path


@pytest.mark.asyncio
@pytest.mark.usefixtures("ctx")
@pytest.mark.parametrize(
    ("fmt", "mime", "ext"),
    [("mermaid", "text/vnd.mermaid", ".mmd"), ("dot", "text/vnd.graphviz", ".dot")],
)
async def test_render_diagram_chain(tmp_path: Path, fmt: str, mime: str, ext: str) -> None:
    workspace = tmp_path / "ws"
    persister = WorkspaceDirPersister(file_storage=LocalFileStorage(workspace), persona_id="astrid")
    tool = make_render_diagram_tool(persister=persister)

    source = "graph TD; A-->B" if fmt == "mermaid" else "digraph { a -> b }"
    result = await tool.execute(source=source, format=fmt)

    assert len(result.artifacts) == 1
    art = result.artifacts[0]
    assert art.mime_type == mime
    assert art.rendered_inline is True
    on_disk = _persisted_path(workspace, art.workspace_path)
    assert on_disk.suffix == ext
    assert on_disk.read_text() == source  # lenient: source persisted verbatim
    sidecar = on_disk.with_name(on_disk.name + ".f5.json")
    meta = json.loads(sidecar.read_text())
    assert meta["producing_spec"] == "28"
    assert meta["type"] == "diagram"
    event = RunEvent.tool_result(step=0, tool_name="render_diagram", result=result)
    assert event.data["artifacts"][0]["mime_type"] == mime


@pytest.mark.asyncio
@pytest.mark.usefixtures("ctx")
async def test_generate_image_chain(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    persister = WorkspaceDirPersister(file_storage=LocalFileStorage(workspace), persona_id="astrid")
    tool = make_generate_image_tool(backend=_FakeImageBackend(), persister=persister)

    result = await tool.execute(prompt="a bicycle")

    assert len(result.artifacts) == 1
    art = result.artifacts[0]
    assert art.mime_type == "image/png"
    assert art.rendered_inline is True
    on_disk = _persisted_path(workspace, art.workspace_path)
    assert on_disk.read_bytes() == b"\x89PNG\r\n\x1a\n"
    # data["images"] reconciled to the persisted ref
    assert result.data["images"][0]["workspace_path"] == art.workspace_path
    event = RunEvent.tool_result(step=0, tool_name="generate_image", result=result)
    assert event.data["artifacts"][0]["workspace_path"] == art.workspace_path


@pytest.mark.asyncio
@pytest.mark.usefixtures("ctx")
async def test_persister_absent_fallback(tmp_path: Path) -> None:
    """Criterion #9 — persister=None ⇒ no artifacts, no crash, old result shape."""
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    tool = make_file_write_tool(sandbox_root=sandbox, persister=None)
    result = await tool.execute(path="out/x.md", content="hi")
    assert result.is_error is False
    assert result.artifacts == ()
    event = RunEvent.tool_result(step=0, tool_name="file_write", result=result)
    assert "artifacts" not in event.data
