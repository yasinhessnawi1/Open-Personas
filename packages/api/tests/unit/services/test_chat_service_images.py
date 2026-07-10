"""Image-workspace cascade — chat_service image resolution + forwarding (Part 1).

Two contracts:

* ``_resolve_turn_images`` reads the uploaded image bytes from the persona
  workspace (via the existing ``image_service`` resolver) and builds the
  runtime-layer :class:`persona_runtime.images.TurnImage` carriers.
* ``stream_chat`` forwards the resolved images to ``loop.turn(images=...)`` —
  the wiring that was missing (the never-landed Spec 13 T12 cascade).
"""

from __future__ import annotations

import struct
import zlib
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from persona.schema.conversation import Conversation, ConversationMessage
from persona_api.schemas import ImageRef
from persona_api.services import chat_service, image_service
from persona_api.services.chat_service import _resolve_turn_images
from persona_api.storage import LocalFileStorage

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from persona.backends import StreamChunk
    from persona_runtime.agentic.events import RunEvent
    from persona_runtime.images import TurnImage


def _minimal_png() -> bytes:
    def chunk(t: bytes, d: bytes) -> bytes:
        crc = zlib.crc32(t + d) & 0xFFFFFFFF
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", crc)

    ihdr = struct.pack(">II", 1, 1) + bytes([8, 2, 0, 0, 0])
    idat_data = zlib.compress(b"\x00\xff\x00\x00")
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", idat_data)
        + chunk(b"IEND", b"")
    )


class TestResolveTurnImages:
    def test_resolves_bytes_and_media_type(self, tmp_path: Path) -> None:
        ref = image_service.upload(
            file_storage=LocalFileStorage(tmp_path),
            owner_id="user_1",
            persona_id="astrid",
            file_bytes=_minimal_png(),
            declared_media_type="image/png",
        )
        body_image = ImageRef(workspace_path=ref.workspace_path, media_type="image/png")

        resolved = _resolve_turn_images(
            file_storage=LocalFileStorage(tmp_path),
            owner_id="user_1",
            persona_id="astrid",
            images=[body_image],
        )

        assert len(resolved) == 1
        ti = resolved[0]
        assert ti.workspace_path == ref.workspace_path
        assert ti.media_type == "image/png"
        assert ti.content_bytes.startswith(b"\x89PNG")

    def test_none_or_empty_returns_empty(self, tmp_path: Path) -> None:
        assert (
            _resolve_turn_images(
                file_storage=LocalFileStorage(tmp_path),
                owner_id="u",
                persona_id="p",
                images=None,
            )
            == []
        )

    def test_missing_workspace_root_returns_empty(self) -> None:
        body_image = ImageRef(workspace_path="uploads/x.png", media_type="image/png")
        assert (
            _resolve_turn_images(
                file_storage=None, owner_id="u", persona_id="p", images=[body_image]
            )
            == []
        )


class _CapturingLoop:
    """Scripted loop that records the ``images`` kwarg it received."""

    def __init__(self) -> None:
        self.images_seen: list[TurnImage] | None = None
        self.documents_seen: list[object] | None = None

    async def turn(
        self,
        conversation: Conversation,
        user_message: str,
        on_event: Callable[[RunEvent], Awaitable[None]] | None = None,  # noqa: ARG002
        *,
        turn_has_image: bool = False,  # noqa: ARG002
        images: list[TurnImage] | None = None,
        documents: list[object] | None = None,
        document_context: object = None,  # noqa: ARG002
    ) -> AsyncIterator[StreamChunk]:
        from persona.backends import StreamChunk

        self.images_seen = images
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

    def heal_orphaned_running(self, **_kwargs: object) -> str | None:
        return None  # R9-022: nothing to heal in this scripted fixture


class _FakeConn:
    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *_a: object) -> None: ...


class _FakeEngine:
    def begin(self) -> _FakeConn:
        return _FakeConn()


@pytest.mark.asyncio
async def test_start_chat_turn_forwards_resolved_images_to_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from persona_api.background.chat_turn_worker import ChatTurnRegistry

    ref = image_service.upload(
        file_storage=LocalFileStorage(tmp_path),
        owner_id="user_1",
        persona_id="astrid",
        file_bytes=_minimal_png(),
        declared_media_type="image/png",
    )
    body_image = ImageRef(workspace_path=ref.workspace_path, media_type="image/png")

    captured = _CapturingLoop()

    # Stub the conversation load so the unit stays Postgres-free; the turn's
    # persistence rides a fake sink.
    monkeypatch.setattr(
        chat_service,
        "_load_conversation",
        lambda _conn, _cid: Conversation(conversation_id="c1", persona_id="astrid", messages=[]),
    )

    async def _build_loop(_pid: str) -> _CapturingLoop:
        return captured

    handle = await chat_service.start_chat_turn(
        rls_engine=_FakeEngine(),  # type: ignore[arg-type]
        sink=_FakeSink(),  # type: ignore[arg-type]
        registry=ChatTurnRegistry(sink=_FakeSink()),  # type: ignore[arg-type]
        loop_builder=_build_loop,  # type: ignore[arg-type]
        owner_id="user_1",
        conversation_id="c1",
        user_message="what is this?",
        channel=None,
        images=[body_image],
        turn_has_image=True,
        workspace_root=tmp_path,
        file_storage=LocalFileStorage(tmp_path),
    )
    await handle.task

    assert captured.images_seen is not None
    assert len(captured.images_seen) == 1
    assert captured.images_seen[0].workspace_path == ref.workspace_path
    assert captured.images_seen[0].content_bytes.startswith(b"\x89PNG")
