"""A replayed image is never read through a link (R9-251, Spec WIN T1.5 M-1).

When history is replayed, an image block carries only a ``workspace_path`` and the
backend reads the bytes from disk. A link planted at that path (or a path that climbs
out) must never send an outside file to a model provider: the read is refused and
nothing is sent. Runs on every platform.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from persona.backends.config import BackendConfig
from persona.backends.ollama import OllamaBackend
from persona.backends.openai_compat import _message_to_anthropic, _resolve_image_bytes
from persona.errors import SandboxViolationError
from persona.schema.content import ImageContent, TextContent
from persona.schema.conversation import ConversationMessage

if TYPE_CHECKING:
    from pathlib import Path

SECRET_PNG = b"\x89PNG\r\n\x1a\nOUTSIDE SECRET"


@pytest.fixture
def planted(tmp_path: Path) -> tuple[Path, str, Path]:
    """``(workspace_root, relative_image_path, outside_file)``: the image path is a link."""
    workspace_root = tmp_path / "workspace"
    uploads = workspace_root / "alice" / "persona-A" / "uploads"
    uploads.mkdir(parents=True)
    outside = tmp_path / "outside.png"
    outside.write_bytes(SECRET_PNG)
    try:
        (uploads / "img.png").symlink_to(outside)
    except OSError as exc:
        if os.environ.get("CI"):
            pytest.fail(f"CI must be able to create symbolic links: {exc}")
        pytest.skip(f"creating a symbolic link needs Developer Mode or elevation here: {exc}")
    return workspace_root, "alice/persona-A/uploads/img.png", outside


def _message(workspace_path: str) -> ConversationMessage:
    return ConversationMessage(
        role="user",
        content=[
            TextContent(text="what is this?"),
            ImageContent(workspace_path=workspace_path, media_type="image/png"),
        ],
        created_at=datetime.now(UTC),
    )


def test_a_linked_image_is_refused_by_the_shared_resolver(
    planted: tuple[Path, str, Path],
) -> None:
    workspace_root, rel, _ = planted
    block = ImageContent(workspace_path=rel, media_type="image/png")
    with pytest.raises((OSError, SandboxViolationError)):
        _resolve_image_bytes(block, workspace_root)


def test_the_anthropic_serialiser_refuses_a_linked_image(
    planted: tuple[Path, str, Path],
) -> None:
    workspace_root, rel, _ = planted
    with pytest.raises((OSError, SandboxViolationError)):
        _message_to_anthropic(
            _message(rel),
            workspace_root=workspace_root,
            supports_vision=True,
            backend="anthropic",
            model="claude-sonnet-4-5",
        )


def test_a_workspace_path_that_climbs_out_is_refused(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    (tmp_path / "outside.png").write_bytes(SECRET_PNG)
    block = ImageContent(workspace_path="../outside.png", media_type="image/png")
    with pytest.raises(SandboxViolationError):
        _resolve_image_bytes(block, workspace_root)


@pytest.mark.asyncio
async def test_ollama_sends_nothing_when_the_image_is_a_link(
    planted: tuple[Path, str, Path],
) -> None:
    workspace_root, rel, _ = planted
    backend = OllamaBackend(
        BackendConfig(provider="ollama", model="llava"),
        use_vision=True,
        workspace_root=workspace_root,
    )
    client = MagicMock()
    client.post = AsyncMock()
    backend._client = client  # noqa: SLF001 - the transport seam the vision tests use
    with pytest.raises((OSError, SandboxViolationError)):
        await backend.chat([_message(rel)])
    client.post.assert_not_awaited()


def test_a_plain_image_is_still_read(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    (workspace_root / "alice").mkdir(parents=True)
    (workspace_root / "alice" / "img.png").write_bytes(SECRET_PNG)
    block = ImageContent(workspace_path="alice/img.png", media_type="image/png")
    assert _resolve_image_bytes(block, workspace_root) == SECRET_PNG
