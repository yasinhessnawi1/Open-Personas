"""Unit tests for the STREAMED authoring generators (spec P0 / T1).

These prove the architectural lock (D-P0-service-yields-events): the service
yields SEMANTIC events — ``("chunk", delta)`` / ``("retry", reason)`` /
``("draft", AuthoringDraft)`` — and is driven here DIRECTLY (no HTTP, no SSE
bytes), proving it is transport-agnostic. The streamed path runs the SAME
``split_response`` → ``_validate_yaml`` → retry path as the blocking path via a
shared ``_finalize`` (D-P0-sse-reuse / D-10-1/3 no drift), and the terminal
``draft`` event always carries the full validated-or-errored ``AuthoringDraft``
(D-P0-errors-on-terminal).

The deduct itself lives in the ROUTE (D-P0-deduct-after-validate); these
service-level tests prove the deduct-ENABLING invariant: a ``draft`` event is
yielded iff a draft was produced, and NEVER on an aborted stream or a provider
failure — so the route (which deducts only after seeing ``draft``) charges
nothing in those cases.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from persona.backends.types import ChatResponse, StreamChunk, TokenUsage
from persona_api.schemas.responses import AuthoringDraft
from persona_api.services.authoring_prompt import AUTHORING_PROMPT_VERSION
from persona_api.services.authoring_service import (
    AuthoringCost,
    AuthoringSampling,
    _price_usage,
    generate_authoring_draft,
    stream_authoring_draft,
    stream_refine_authoring_draft,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

# Reuse the exact YAML fixtures the blocking-path tests use (drift guard).
GOOD_YAML = """\
schema_version: "1.0"
identity:
  name: Lex
  role: Legal information assistant
  background: A general legal-information assistant for everyday questions.
  constraints:
    - Do not fabricate information; say when you don't know."""

# Top-level `hobbies` is not in the schema -> extra="forbid" rejects it.
BAD_YAML = GOOD_YAML + "\nhobbies:\n  - chess"

GOOD_WITH_QUESTIONS = (
    GOOD_YAML + '\n---QUESTIONS---\n[{"section": "identity", "question": "Which legal area?"}]'
)


class _StreamingScriptedBackend:
    """A ChatBackend stand-in whose ``chat_stream`` replays canned content.

    Mirrors the D-02-5 protocol shape (``def -> AsyncIterator[StreamChunk]``):
    a plain ``def`` that records the call + sampling and returns an async
    generator. Each canned content is fragmented into several deltas (proving
    accumulation + ordering), with the last chunk carrying ``is_final=True`` +
    usage. ``raises=True`` makes the stream raise mid-iteration (provider error).
    """

    def __init__(self, *contents: str, raises: bool = False) -> None:
        self._contents = list(contents)
        self.calls: list[list[object]] = []
        self.sampling: list[dict[str, object]] = []
        self._raises = raises

    @property
    def provider_name(self) -> str:
        # Spec M3 (T2a): _price_usage reads provider/model to price the turn.
        return "mock"

    @property
    def model_name(self) -> str:
        return "mock"

    def chat_stream(self, messages: list[object], **kwargs: object) -> AsyncIterator[StreamChunk]:
        self.calls.append(list(messages))
        self.sampling.append(
            {
                "temperature": kwargs.get("temperature"),
                "top_p": kwargs.get("top_p"),
                "top_k": kwargs.get("top_k"),
            }
        )
        content = self._contents[len(self.calls) - 1]
        raises = self._raises

        async def _gen() -> AsyncIterator[StreamChunk]:
            if raises:
                raise RuntimeError("provider boom")
            step = max(1, len(content) // 3)
            parts = [content[i : i + step] for i in range(0, len(content), step)] or [""]
            for part in parts[:-1]:
                yield StreamChunk(delta=part, is_final=False)
            yield StreamChunk(
                delta=parts[-1],
                is_final=True,
                usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            )

        return _gen()

    async def chat(self, messages: list[object], **kwargs: object) -> ChatResponse:
        """Blocking arm of the same stand-in — lets the equivalence test feed the
        SAME canned content through both the blocking and streamed paths."""
        self.calls.append(list(messages))
        self.sampling.append(
            {
                "temperature": kwargs.get("temperature"),
                "top_p": kwargs.get("top_p"),
                "top_k": kwargs.get("top_k"),
            }
        )
        return ChatResponse(
            content=self._contents[len(self.calls) - 1],
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            model="mock",
            provider="mock",
            latency_ms=0.0,
        )


async def _drain(stream: AsyncIterator[object]) -> list[tuple[str, object]]:
    """Collect every semantic event the service yields (no HTTP in the loop)."""
    return [event async for event in stream]  # type: ignore[misc]


def _kinds(events: list[tuple[str, object]]) -> list[str]:
    return [kind for kind, _ in events]


def _text(events: list[tuple[str, object]]) -> str:
    return "".join(str(payload) for kind, payload in events if kind == "chunk")


def _drafts(events: list[tuple[str, object]]) -> list[AuthoringDraft]:
    return [payload for kind, payload in events if kind == "draft"]  # type: ignore[misc]


@pytest.mark.asyncio
async def test_stream_emits_chunks_then_one_terminal_draft() -> None:
    backend = _StreamingScriptedBackend(GOOD_WITH_QUESTIONS)
    events = await _drain(stream_authoring_draft(backend, "lawyer", [], []))  # type: ignore[arg-type]

    # transport-agnostic: we drove the async generator directly, no SSE bytes.
    assert "chunk" in _kinds(events)
    assert "retry" not in _kinds(events)
    assert _kinds(events)[-1] == "draft"  # terminal event is the draft
    assert len(_drafts(events)) == 1
    # accumulated chunk deltas reconstruct the model output verbatim.
    assert _text(events) == GOOD_WITH_QUESTIONS
    draft = _drafts(events)[0]
    assert draft.errors is None
    assert draft.prompt_version == AUTHORING_PROMPT_VERSION
    assert [q.section for q in draft.questions] == ["identity"]
    assert len(backend.calls) == 1


@pytest.mark.asyncio
async def test_stream_terminal_draft_equals_blocking_draft() -> None:
    # THE shared-_finalize proof: identical model output → byte-identical draft
    # on both paths. No drift on the D-10-1/3 parse→validate→retry contract.
    streamed = await _drain(
        stream_authoring_draft(_StreamingScriptedBackend(GOOD_WITH_QUESTIONS), "lawyer", [], [])  # type: ignore[arg-type]
    )
    blocking = await generate_authoring_draft(
        _StreamingScriptedBackend(GOOD_WITH_QUESTIONS),  # type: ignore[arg-type]
        "lawyer",
        [],
        [],
    )
    terminal = _drafts(streamed)[0]
    assert terminal.model_dump() == blocking.model_dump()


@pytest.mark.asyncio
async def test_stream_invalid_then_valid_emits_retry_then_clean_draft() -> None:
    backend = _StreamingScriptedBackend(BAD_YAML, GOOD_YAML)
    events = await _drain(stream_authoring_draft(backend, "lawyer", [], []))  # type: ignore[arg-type]

    kinds = _kinds(events)
    # a visible retry event sits between attempt-1 chunks and attempt-2 chunks.
    assert "retry" in kinds
    retry_at = kinds.index("retry")
    assert "chunk" in kinds[:retry_at]  # attempt 1 streamed
    assert "chunk" in kinds[retry_at + 1 :]  # attempt 2 re-streamed
    assert kinds[-1] == "draft"
    draft = _drafts(events)[0]
    assert draft.errors is None  # repaired
    assert len(backend.calls) == 2
    # the re-stream repair attempt is deterministic (D-10-3): temp 0.0.
    assert backend.sampling[1] == {"temperature": 0.0, "top_p": None, "top_k": None}


@pytest.mark.asyncio
async def test_stream_retry_exhausted_puts_errors_on_terminal_draft() -> None:
    # bad-then-bad: the terminal draft still arrives, carrying errors for the
    # form to fix — NEVER a half-formed draft, and never a raise (D-P0-errors-on-terminal).
    backend = _StreamingScriptedBackend(BAD_YAML, BAD_YAML)
    events = await _drain(stream_authoring_draft(backend, "lawyer", [], []))  # type: ignore[arg-type]

    assert _kinds(events)[-1] == "draft"
    draft = _drafts(events)[0]
    assert draft.errors is not None
    assert any("hobbies" in err for err in draft.errors)
    assert len(backend.calls) == 2


@pytest.mark.asyncio
async def test_stream_provider_failure_yields_no_draft() -> None:
    # A provider error propagates and NO draft event is yielded → the route
    # (deduct-after-draft) charges nothing on a failed generation (D-08-6 lineage).
    backend = _StreamingScriptedBackend(GOOD_YAML, raises=True)
    seen: list[tuple[str, object]] = []

    async def _drive() -> None:
        async for event in stream_authoring_draft(backend, "lawyer", [], []):  # type: ignore[arg-type]
            seen.append(event)

    with pytest.raises(RuntimeError, match="provider boom"):
        await _drive()
    assert all(kind != "draft" for kind, _ in seen)


@pytest.mark.asyncio
async def test_stream_aborted_midway_yields_no_draft() -> None:
    # Client disconnect: close the generator after one chunk. No draft event is
    # ever produced → the route never reaches the deduct (no charge on abort).
    backend = _StreamingScriptedBackend(GOOD_WITH_QUESTIONS)
    stream = stream_authoring_draft(backend, "lawyer", [], [])  # type: ignore[arg-type]
    seen: list[tuple[str, object]] = []
    first = await stream.__anext__()
    seen.append(first)
    await stream.aclose()  # type: ignore[attr-defined]
    assert first[0] == "chunk"
    assert all(kind != "draft" for kind, _ in seen)


@pytest.mark.asyncio
async def test_stream_first_attempt_creative_retry_deterministic() -> None:
    # The D-10-3 sampling split survives in the streamed path.
    backend = _StreamingScriptedBackend(BAD_YAML, GOOD_YAML)
    sampling = AuthoringSampling(temperature=0.9, top_p=0.95, top_k=60)
    await _drain(stream_authoring_draft(backend, "lawyer", [], [], sampling=sampling))  # type: ignore[arg-type]
    assert backend.sampling[0] == {"temperature": 0.9, "top_p": 0.95, "top_k": 60}
    assert backend.sampling[1] == {"temperature": 0.0, "top_p": None, "top_k": None}


@pytest.mark.asyncio
async def test_stream_refine_threads_question_and_answer() -> None:
    backend = _StreamingScriptedBackend(GOOD_YAML)
    events = await _drain(
        stream_refine_authoring_draft(
            backend,  # type: ignore[arg-type]
            current_yaml=GOOD_YAML,
            question="Which legal area?",
            answer="Tenancy law.",
            available_tools=[],
            available_skills=[],
        )
    )
    assert _kinds(events)[-1] == "draft"
    assert _drafts(events)[0].errors is None
    sent = backend.calls[0]
    assert any("Which legal area?" in m.content for m in sent)  # type: ignore[attr-defined]
    assert any(m.content == "Tenancy law." for m in sent)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Spec M3 (T2a) — real usage/cost surfacing (the ("cost", AuthoringCost) event,
# mirroring the chat loop's last_turn_cost_cents/last_turn_cost_basis).
# ---------------------------------------------------------------------------


class _PriceableBackend:
    """Streams the given content and reports a priceable served model + real usage.

    ``anthropic`` / ``claude-sonnet-5`` is in the static pricing table
    (0.30 / 1.50 ¢ per 1k tok), so 2000 prompt + 800 completion tokens per attempt
    cost ``2·0.30 + 0.8·1.50 = 1.8¢`` (basis ``estimate_static``).
    """

    def __init__(
        self,
        *contents: str,
        provider: str = "anthropic",
        model: str = "claude-sonnet-5",
        prompt_tokens: int = 2000,
        completion_tokens: int = 800,
        cost_usd: float | None = None,
    ) -> None:
        self._contents = list(contents)
        self.calls = 0
        self._provider = provider
        self._model = model
        self._prompt = prompt_tokens
        self._completion = completion_tokens
        self._cost_usd = cost_usd

    @property
    def provider_name(self) -> str:
        return self._provider

    @property
    def model_name(self) -> str:
        return self._model

    def chat_stream(self, messages: list[object], **_kwargs: object) -> AsyncIterator[StreamChunk]:  # noqa: ARG002
        content = self._contents[self.calls]
        self.calls += 1
        prompt, completion, cost_usd = self._prompt, self._completion, self._cost_usd

        async def _gen() -> AsyncIterator[StreamChunk]:
            yield StreamChunk(delta=content, is_final=False)
            yield StreamChunk(
                delta="",
                is_final=True,
                usage=TokenUsage(
                    prompt_tokens=prompt,
                    completion_tokens=completion,
                    total_tokens=prompt + completion,
                    cost_usd=cost_usd,
                ),
            )

        return _gen()


def _costs(events: list[tuple[str, object]]) -> list[AuthoringCost]:
    return [payload for kind, payload in events if kind == "cost"]  # type: ignore[misc]


def test_price_usage_mirrors_compute_turn_cost_for_a_priceable_model() -> None:
    backend = _PriceableBackend()
    cents, basis, prompt, completion = _price_usage(
        backend, TokenUsage(prompt_tokens=2000, completion_tokens=800, total_tokens=2800), None
    )  # type: ignore[arg-type]
    assert cents == pytest.approx(1.8)  # 2·0.30 + 0.8·1.50
    assert basis == "estimate_static"
    assert (prompt, completion) == (2000, 800)


def test_price_usage_prefers_openrouter_actual() -> None:
    backend = _PriceableBackend(provider="openrouter", model="z-ai/glm-4.6")
    usage = TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15, cost_usd=0.02)
    cents, basis, _p, _c = _price_usage(backend, usage, None)
    assert cents == pytest.approx(2.0)  # 0.02 USD → 2.0¢
    assert basis == "actual_openrouter"


def test_price_usage_none_usage_is_unpriced() -> None:
    assert _price_usage(_PriceableBackend(), None, None) == (0.0, "unpriced", 0, 0)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_cost_event_carries_the_real_cost_before_the_draft() -> None:
    backend = _PriceableBackend(GOOD_WITH_QUESTIONS)
    events = await _drain(stream_authoring_draft(backend, "lawyer", [], []))  # type: ignore[arg-type]
    kinds = _kinds(events)
    # Exactly one cost event, emitted immediately before the terminal draft.
    assert kinds.count("cost") == 1
    assert kinds[-2:] == ["cost", "draft"]
    cost = _costs(events)[0]
    assert cost.cost_cents == pytest.approx(1.8)
    assert cost.cost_basis == "estimate_static"
    assert (cost.prompt_tokens, cost.completion_tokens) == (2000, 800)


@pytest.mark.asyncio
async def test_cost_event_sums_both_attempts_on_retry() -> None:
    # Invalid then valid → two paid generations; the surfaced cost is the SUM.
    backend = _PriceableBackend(BAD_YAML, GOOD_WITH_QUESTIONS)
    events = await _drain(stream_authoring_draft(backend, "lawyer", [], []))  # type: ignore[arg-type]
    assert _kinds(events).count("cost") == 1  # still ONE cost event (the total)
    cost = _costs(events)[0]
    assert cost.cost_cents == pytest.approx(3.6)  # 1.8 × 2 attempts
    assert (cost.prompt_tokens, cost.completion_tokens) == (4000, 1600)
    assert backend.calls == 2


@pytest.mark.asyncio
async def test_cost_event_unpriced_when_the_model_is_off_table() -> None:
    backend = _PriceableBackend(GOOD_WITH_QUESTIONS, provider="mock", model="mock")
    events = await _drain(stream_authoring_draft(backend, "lawyer", [], []))  # type: ignore[arg-type]
    cost = _costs(events)[0]
    assert cost.cost_cents == 0.0
    assert cost.cost_basis == "unpriced"
