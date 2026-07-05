"""Spec K8 T5 — the Summarizer seam (K8-D-10; acceptance 3 on the MECHANISM).

The structural centerpiece: :func:`assemble_summarizer_input` is the only path
from memory to the summarizer, and it REFUSES gists — so from-originals holds
on the input mechanism itself, independent of the store refusing gist members
(T4). Plus: the deterministic stub, the tier adapter's output discipline
(empty ⇒ domain error; over-long ⇒ sentence-boundary truncation; provider
exceptions ⇒ SummarizerError, never leaked raw), and Protocol satisfaction.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from persona.errors import SummarizerError
from persona.schema.chunks import PersonaChunk, mint_chunk_id
from persona.skills import count_tokens
from persona.stores.summarizer import (
    StubSummarizer,
    Summarizer,
    TierSummarizer,
    assemble_summarizer_input,
)

_NOW = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)


def _raw(text: str, *, minutes: int = 0) -> PersonaChunk:
    return PersonaChunk(
        id=mint_chunk_id("p1", "episodic"),
        text=text,
        created_at=_NOW + timedelta(minutes=minutes),
    )


def _gist_chunk() -> PersonaChunk:
    return PersonaChunk(
        id="p1::episodic_gist::deadbeef",
        text="a prior summary",
        created_at=_NOW,
        band=1,
        member_ids=("m1",),
    )


# --- acceptance 3: from-originals proven on the assembly mechanism -------------


def test_assembly_joins_raw_texts_oldest_first() -> None:
    newer = _raw("USER: second\nASSISTANT: ok", minutes=5)
    older = _raw("USER: first\nASSISTANT: hi", minutes=0)
    out = assemble_summarizer_input([newer, older])
    assert out.index("first") < out.index("second")  # chronological
    assert "USER: first" in out
    assert "USER: second" in out


def test_assembly_refuses_a_gist_by_member_ids() -> None:
    with pytest.raises(SummarizerError, match="from-originals"):
        assemble_summarizer_input([_raw("ok"), _gist_chunk()])


def test_assembly_refuses_a_gist_by_id_marker_even_without_members() -> None:
    sneaky = PersonaChunk(id="p1::episodic_gist::no-members-set", text="looks raw", created_at=_NOW)
    with pytest.raises(SummarizerError, match="from-originals"):
        assemble_summarizer_input([sneaky])


def test_assembly_refuses_empty_input() -> None:
    with pytest.raises(SummarizerError, match="non-empty"):
        assemble_summarizer_input([])


# --- the stub: deterministic, protocol-satisfying --------------------------------


@pytest.mark.asyncio
async def test_stub_is_deterministic_and_respects_the_budget() -> None:
    stub = StubSummarizer()
    content = ". ".join(f"Sentence number {i} with several words in it" for i in range(50))
    first = await stub.summarize(content, target_tokens=40)
    second = await stub.summarize(content, target_tokens=40)
    assert first == second  # deterministic — the engine's idempotency depends on it
    assert count_tokens(first) <= 40  # noqa: PLR2004
    assert first  # never empty for non-empty input


@pytest.mark.asyncio
async def test_stub_rejects_empty_content() -> None:
    with pytest.raises(SummarizerError, match="nothing to summarize"):
        await StubSummarizer().summarize("   ", target_tokens=40)


def test_both_implementations_satisfy_the_protocol() -> None:
    assert isinstance(StubSummarizer(), Summarizer)
    assert isinstance(TierSummarizer(backend=_FakeBackend("x")), Summarizer)


# --- the tier adapter: output discipline ------------------------------------------


class _FakeBackend:
    """Minimal ChatBackend double returning a fixed reply."""

    provider_name = "fake"

    def __init__(self, reply: str, *, explode: bool = False) -> None:
        self._reply = reply
        self._explode = explode
        self.prompts: list[str] = []

    async def chat(self, *, messages: list[Any], **_: object) -> Any:  # noqa: ANN401
        from types import SimpleNamespace

        if self._explode:
            msg = "provider 503"
            raise RuntimeError(msg)
        self.prompts.append(messages[0].content)
        return SimpleNamespace(content=self._reply)


@pytest.mark.asyncio
async def test_tier_adapter_prompts_with_the_raw_content_and_budget() -> None:
    backend = _FakeBackend("They planned the Oslo move.")
    adapter = TierSummarizer(backend=backend)
    content = assemble_summarizer_input([_raw("USER: moving to Oslo\nASSISTANT: nice")])
    out = await adapter.summarize(content, target_tokens=120)
    assert out == "They planned the Oslo move."
    assert "moving to Oslo" in backend.prompts[0]
    assert "120" in backend.prompts[0]  # the budget is part of the instruction
    assert "key details" in backend.prompts[0]  # the RAPTOR-shape instruction


@pytest.mark.asyncio
async def test_tier_adapter_raises_domain_error_on_empty_output() -> None:
    adapter = TierSummarizer(backend=_FakeBackend("   "))
    with pytest.raises(SummarizerError, match="empty output"):
        await adapter.summarize("some content", target_tokens=120)


@pytest.mark.asyncio
async def test_tier_adapter_wraps_provider_failures_as_domain_errors() -> None:
    adapter = TierSummarizer(backend=_FakeBackend("x", explode=True))
    with pytest.raises(SummarizerError, match="backend call failed"):
        await adapter.summarize("some content", target_tokens=120)


@pytest.mark.asyncio
async def test_tier_adapter_truncates_overlong_output_at_a_sentence_boundary() -> None:
    runaway = ". ".join(f"Overlong model sentence number {i} padding words" for i in range(80))
    adapter = TierSummarizer(backend=_FakeBackend(runaway))
    out = await adapter.summarize("content", target_tokens=30)
    assert count_tokens(out) <= 60  # noqa: PLR2004 — the 2× cap
    assert out.endswith(".")  # sentence boundary, not a mid-word chop
