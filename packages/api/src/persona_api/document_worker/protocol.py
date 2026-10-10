"""The wire between the api and the document parse worker: framed JSON over pipes.

Each message is a 4-byte big-endian length followed by that many bytes of UTF-8 JSON.
Only plain data crosses: a job is a path, a render directory and a DPI; a reply is the
parse result as JSON, page image paths, or a typed error. No pickle, so neither side
can make the other run code by what it sends.

Imported by the worker process, so it imports only the standard library.
"""

from __future__ import annotations

import json
import struct
from typing import IO, Any

__all__ = [
    "MAX_FRAME_BYTES",
    "FrameError",
    "read_frame",
    "write_frame",
]

#: The largest message either side accepts. A reply carries a document's whole text,
#: at most :data:`persona.documents.parsers.MAX_DOCUMENT_TEXT_CHARS` (four million
#: characters). In JSON a character costs at most 6 bytes (a control character escaped as
#: ``\u00XX``; anything else is at most 4 bytes of UTF-8): 24 MB, plus the sections'
#: own JSON, about 60 bytes each for the 200,000 a docx may have: 12 MB. 48 MiB holds
#: the largest legal reply with room to spare; a test builds that reply and checks.
#: The worker refuses to send more, and the api refuses to read more.
MAX_FRAME_BYTES: int = 48 * 1024 * 1024

_HEADER = struct.Struct(">I")


class FrameError(Exception):
    """The other side closed the pipe, or sent something that is not a frame."""


def write_frame(stream: IO[bytes], message: dict[str, Any]) -> None:
    """Write one message and flush it."""
    # ``replace``: a lone surrogate a parser let through becomes "?" instead of making
    # the reply unencodable.
    body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8", "replace")
    if len(body) > MAX_FRAME_BYTES:
        msg = f"message of {len(body)} bytes is over the frame limit"
        raise FrameError(msg)
    stream.write(_HEADER.pack(len(body)) + body)
    stream.flush()


def _read_exactly(stream: IO[bytes], size: int) -> bytes | None:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_frame(stream: IO[bytes]) -> dict[str, Any] | None:
    """Read one message; ``None`` when the stream ended cleanly before a new frame.

    Raises:
        FrameError: The stream ended inside a frame, the frame is over
            :data:`MAX_FRAME_BYTES`, or its body is not a JSON object.
    """
    header = _read_exactly(stream, _HEADER.size)
    if header is None:
        return None
    (size,) = _HEADER.unpack(header)
    if size > MAX_FRAME_BYTES:
        msg = f"frame of {size} bytes is over the frame limit"
        raise FrameError(msg)
    body = _read_exactly(stream, size)
    if body is None:
        msg = "the stream ended inside a frame"
        raise FrameError(msg)
    try:
        message = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, MemoryError) as exc:
        # F8 (second review): nesting deep enough to exhaust the decoder's stack, or a
        # body too large to decode, is a broken frame like any other, never an error
        # that escapes past the caller's handling.
        msg = "the frame is not JSON the api can read"
        raise FrameError(msg) from exc
    if not isinstance(message, dict):
        msg = "the frame is not a JSON object"
        raise FrameError(msg)
    return message
