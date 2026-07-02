"""The streaming-safe feeling-tag converter (N5-D-1, N5-D-2) — the criterion-3 core.

A pure, stateful filter that replaces ``{{#tag}}`` feeling-tags **in place** as a token
stream arrives — with the mapped emoji (chat, :attr:`ConvertMode.EMOJI`) or with nothing
(voice, :attr:`ConvertMode.STRIP`). One tested implementation, applied at every streaming
boundary (chat ``loop.turn()`` before the history write-back, voice ``reply_producer``
before TTS), so the guarantee is path-independent.

**The guarantee (criterion 3):** a raw ``{{#…}}`` NEVER reaches the user — not mid-stream,
not when a tag is split across chunks, not when malformed/unknown, not when left unclosed
at stream end. The mechanism is a trailing-prefix buffer: emit all provably-safe text
immediately, and hold back only the trailing run that could still be building toward the
``{{#`` sentinel, until the next chunk (or :meth:`flush`) disambiguates it.

**Flush contract (N5-D-2):** at stream end, a trailing ``{`` / ``{{`` that never reached the
``{{#`` sentinel is emitted **literally** (real braces, preserved); an *unclosed* ``{{#name…``
is **stripped** (never leaked). Malformed/unknown closed tags are stripped mid-stream.

The filter is O(n), allocation-light, and idempotent on already-safe text. It is *not*
concurrency-safe: one instance per stream (each turn constructs its own).
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from persona_runtime.emotional.vocabulary import lookup_feeling

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["ConvertMode", "FeelingTagConverter", "convert_text"]

_SENTINEL = "{{#"
_CLOSE = "}}"


class ConvertMode(StrEnum):
    """How a resolved feeling-tag is rendered to the user."""

    #: Chat/text: substitute the tag's canonical emoji in place.
    EMOJI = "emoji"
    #: Voice: strip the tag entirely (TTS must never speak a tag). Expressivity on
    #: the voice path is V12's job; N5 only guarantees the tag never escapes.
    STRIP = "strip"


class FeelingTagConverter:
    """Stateful streaming filter converting/stripping feeling-tags, leak-free.

    Usage: ``conv = FeelingTagConverter(mode)``; feed each delta through
    :meth:`feed` and emit its return; call :meth:`flush` once at stream end and
    emit its return. Everything returned is safe to show/speak immediately.

    **V12 capture seam (V12-D-2).** ``on_feeling`` is an optional callback that
    fires with the tag *name* the moment a **known** feeling-tag is recognised —
    a pure post-decision **notification**. It does not touch the strip/substitute
    output or the trailing-prefix buffering, so N5's criterion-3 guarantee is
    preserved by *reuse*: unknown/malformed tags are still stripped and are
    **never captured**, and an unset callback (chat, and voice before V12) is
    byte-identical to the pre-V12 filter. V12's voice path passes this to read
    the persona's declared stance while the tag is still stripped from TTS. The
    callback **MUST NOT raise** (the voice caller passes a pure appender); it runs
    on the streaming hot path, so it must also be cheap.
    """

    __slots__ = ("_buf", "_mode", "_on_feeling")

    def __init__(
        self,
        mode: ConvertMode,
        *,
        on_feeling: Callable[[str], None] | None = None,
    ) -> None:
        self._mode = mode
        self._buf = ""
        self._on_feeling = on_feeling

    def feed(self, delta: str) -> str:
        """Consume one stream delta; return the safe text ready to emit now."""
        self._buf += delta
        return self._drain(final=False)

    def flush(self) -> str:
        """Drain the tail at stream end; return the final safe text.

        Resets the converter so the instance is reusable for a fresh stream.
        """
        out = self._drain(final=True)
        self._buf = ""
        return out

    def _drain(self, *, final: bool) -> str:
        out: list[str] = []
        buf = self._buf
        while buf:
            idx = buf.find(_SENTINEL)
            if idx == -1:
                # No sentinel present. Emit everything except a trailing run that
                # could still grow into "{{#" ("{" or "{{"); at stream end those
                # are just literal braces (N5-D-2) and are emitted verbatim.
                if final:
                    out.append(buf)
                    buf = ""
                else:
                    keep = _trailing_sentinel_prefix_len(buf)
                    cut = len(buf) - keep
                    out.append(buf[:cut])
                    buf = buf[cut:]
                break

            # Safe text precedes the sentinel — always emittable.
            out.append(buf[:idx])
            rest = buf[idx:]
            close = rest.find(_CLOSE, len(_SENTINEL))
            if close == -1:
                # In-progress tag, no close yet. Hold it (or, at stream end, an
                # unclosed tag is malformed → strip it, never leak — N5-D-2).
                buf = "" if final else rest
                break

            body = rest[len(_SENTINEL) : close]
            emoji = lookup_feeling(body)
            if emoji is not None:
                # V12-D-2: observe the recognised tag (pure notification, any mode)
                # BEFORE the substitute/strip decision below. Fires only for KNOWN
                # tags, so an unknown/malformed body is never captured — the strip
                # guarantee is unchanged either way.
                if self._on_feeling is not None:
                    self._on_feeling(body)
                if self._mode is ConvertMode.EMOJI:
                    out.append(emoji)
            # STRIP mode, or an unknown/malformed body (emoji is None) → emit
            # nothing: the tag is consumed and never shown raw (criterion 3).
            buf = rest[close + len(_CLOSE) :]

        self._buf = buf
        return "".join(out)


def convert_text(text: str, mode: ConvertMode = ConvertMode.EMOJI) -> str:
    """Convert/strip feeling-tags in a **complete** string (non-streaming).

    For text already whole (e.g. the persisted ``assistant_text``, N5-D-4) where
    there is no chunk-splitting to worry about. Idempotent on already-safe text.
    """
    conv = FeelingTagConverter(mode)
    return conv.feed(text) + conv.flush()


def _trailing_sentinel_prefix_len(buf: str) -> int:
    """Length of the trailing run of ``buf`` that is a proper prefix of ``{{#``.

    Only ``{`` (1) or ``{{`` (2) can still grow into the sentinel, so at most two
    characters are ever held back in the no-open-tag case.
    """
    for k in (2, 1):
        if buf.endswith(_SENTINEL[:k]):
            return k
    return 0
