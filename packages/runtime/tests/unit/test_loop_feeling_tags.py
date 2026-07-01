"""N5-A7 — feeling-tags on the chat/agentic path (N5-D-1/D-4/D-7).

Proves the wiring, end to end through ``loop.turn()``: the DISPLAY stream converts
tags→emojis (never a raw ``{{#…}}`` in any chunk, including split across deltas), and
episodic HISTORY stores the converted text (what the user saw), never the raw tag.
"""

from __future__ import annotations

import pytest
from _fakes import ScriptedBackend, ScriptedRound  # type: ignore[import-not-found]
from test_loop import _conv, _make_loop  # type: ignore[import-not-found]


def _episodic_text(stores: dict[str, object]) -> str:
    writes = stores["episodic"].writes  # type: ignore[attr-defined]
    assert len(writes) == 1
    return writes[0][0].text


@pytest.mark.asyncio
async def test_whole_tag_converts_in_stream_and_history() -> None:
    backend = ScriptedBackend([ScriptedRound(text_deltas=["I'm ", "{{#happy}}", " to help"])])
    loop, stores, _writer = _make_loop(backend)

    chunks = [c async for c in loop.turn(_conv(0), "hi")]

    display = "".join(c.delta for c in chunks)
    assert display == "I'm 😊 to help"
    assert all("{{#" not in c.delta for c in chunks)
    # History stores the converted text — never the raw tag.
    persisted = _episodic_text(stores)
    assert "😊" in persisted
    assert "{{#" not in persisted


@pytest.mark.asyncio
async def test_tag_split_across_deltas_never_leaks() -> None:
    backend = ScriptedBackend([ScriptedRound(text_deltas=["I feel {{#", "hap", "py}} now"])])
    loop, stores, _writer = _make_loop(backend)

    chunks = [c async for c in loop.turn(_conv(0), "hi")]

    # No individual chunk ever carries a raw sentinel fragment.
    assert all("{{#" not in c.delta for c in chunks)
    assert "".join(c.delta for c in chunks) == "I feel 😊 now"
    assert "{{#" not in _episodic_text(stores)


@pytest.mark.asyncio
async def test_unknown_tag_stripped_everywhere() -> None:
    backend = ScriptedBackend([ScriptedRound(text_deltas=["hi ", "{{#nonsense}}", " bye"])])
    loop, stores, _writer = _make_loop(backend)

    chunks = [c async for c in loop.turn(_conv(0), "hi")]

    assert "".join(c.delta for c in chunks) == "hi  bye"
    assert "{{#" not in _episodic_text(stores)


@pytest.mark.asyncio
async def test_multiple_tags_one_turn() -> None:
    backend = ScriptedBackend(
        [ScriptedRound(text_deltas=["{{#proud_of_you}}", " — and ", "{{#grateful}}"])]
    )
    loop, stores, _writer = _make_loop(backend)

    chunks = [c async for c in loop.turn(_conv(0), "hi")]

    assert "".join(c.delta for c in chunks) == "😌 — and 🙏"
    persisted = _episodic_text(stores)
    assert "😌" in persisted
    assert "🙏" in persisted
    assert "{{#" not in persisted


@pytest.mark.asyncio
async def test_plain_turn_unaffected() -> None:
    # No tags → byte-identical passthrough (no regression to the streaming path).
    backend = ScriptedBackend([ScriptedRound(text_deltas=["Hello, ", "Astrid ", "here."])])
    loop, stores, _writer = _make_loop(backend)

    chunks = [c async for c in loop.turn(_conv(0), "hi")]

    assert "".join(c.delta for c in chunks) == "Hello, Astrid here."
    assert "Hello, Astrid here." in _episodic_text(stores)
