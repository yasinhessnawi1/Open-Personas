"""Contract tests — every ``ChatBackend`` impl honours the Protocol.

Parametrised across all three concrete backends (OpenAICompatibleBackend
with anthropic + openai variants, OllamaBackend, HFLocalBackend). One
factory per backend builds a mocked instance so no real provider calls
happen.

Adding a new backend in a future spec only requires:
  1. Add a row to ``_BACKENDS`` below.
  2. Implement a factory that returns ``(backend, expected_provider)``.

Marked with ``@pytest.mark.contract`` so they run on demand:
    uv run pytest -m contract
"""

# ruff: noqa: ANN401, SLF001, ARG001, ARG002, ARG003 — mock/stub fixtures use Any types and unused args

from __future__ import annotations

import sys
import types
from collections.abc import Callable, Iterator  # noqa: TC003 — used at runtime in _BACKENDS type
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from persona.backends import (
    BackendConfig,
    ChatBackend,
    ChatResponse,
    StreamChunk,
)
from persona.backends.errors import AuthenticationError
from persona.backends.ollama import OllamaBackend
from persona.backends.openai_compat import OpenAICompatibleBackend
from persona.backends.types import ToolSpec
from persona.schema.conversation import ConversationMessage
from pydantic import SecretStr

pytestmark = pytest.mark.contract


def _user(text: str) -> ConversationMessage:
    return ConversationMessage(role="user", content=text, created_at=datetime.now(UTC))


# -----------------------------------------------------------------------------
# Backend factories — one per (provider, variant) pair
# -----------------------------------------------------------------------------


def _make_anthropic_backend() -> OpenAICompatibleBackend:
    backend = OpenAICompatibleBackend(
        BackendConfig(provider="anthropic", model="claude-test", api_key=SecretStr("k"))
    )
    # Mock messages.create.
    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = "hi"
    usage = MagicMock()
    usage.input_tokens = 3
    usage.output_tokens = 2
    response = MagicMock()
    response.content = [text_block]
    response.model = "claude-test"
    response.usage = usage
    backend._anthropic.messages.create = AsyncMock(return_value=response)  # type: ignore[union-attr]

    # Mock messages.stream.
    text_event = MagicMock()
    text_event.type = "content_block_delta"
    text_event.delta = MagicMock(type="text_delta", text="hi")

    class _FakeStream:
        async def __aenter__(self) -> _FakeStream:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        def __aiter__(self) -> Any:
            async def gen() -> Any:
                yield text_event

            return gen()

        async def get_final_message(self) -> Any:
            return response

    backend._anthropic.messages.stream = MagicMock(return_value=_FakeStream())  # type: ignore[union-attr]
    return backend


def _make_openai_backend() -> OpenAICompatibleBackend:
    backend = OpenAICompatibleBackend(
        BackendConfig(provider="openai", model="gpt-test", api_key=SecretStr("k"))
    )
    # Mock non-streaming.
    message = MagicMock()
    message.content = "hi"
    message.tool_calls = []
    choice = MagicMock()
    choice.message = message
    usage = MagicMock()
    usage.prompt_tokens = 3
    usage.completion_tokens = 2
    response = MagicMock()
    response.choices = [choice]
    response.usage = usage
    response.model = "gpt-test"

    # Mock streaming.
    async def stream_iter() -> Any:
        chunk1 = MagicMock()
        chunk1.choices = [MagicMock(delta=MagicMock(content="hi", tool_calls=[]))]
        chunk1.usage = None
        yield chunk1
        usage_chunk = MagicMock()
        usage_chunk.choices = []
        usage_chunk.usage = usage
        yield usage_chunk

    async def create(**kwargs: Any) -> Any:
        if kwargs.get("stream"):
            return stream_iter()
        return response

    backend._openai.chat.completions.create = AsyncMock(side_effect=create)  # type: ignore[union-attr]
    return backend


def _make_ollama_backend() -> OllamaBackend:
    backend = OllamaBackend(BackendConfig(provider="ollama", model="llama3"))
    client = MagicMock()
    # Non-streaming.
    response = MagicMock()
    response.status_code = 200
    response.is_success = True
    response.headers = {"content-type": "application/json"}
    response.json = MagicMock(
        return_value={
            "message": {"role": "assistant", "content": "hi"},
            "done": True,
            "prompt_eval_count": 3,
            "eval_count": 2,
        }
    )
    client.post = AsyncMock(return_value=response)

    # Streaming.
    class _StreamCtx:
        async def __aenter__(self) -> Any:
            r = MagicMock()
            r.status_code = 200
            r.is_success = True
            r.headers = {}

            async def aiter_lines() -> Any:
                import json as _json

                yield _json.dumps({"message": {"content": "hi"}, "done": False})
                yield _json.dumps(
                    {
                        "message": {"content": ""},
                        "done": True,
                        "prompt_eval_count": 3,
                        "eval_count": 2,
                    }
                )

            r.aiter_lines = aiter_lines
            return r

        async def __aexit__(self, *args: Any) -> None:
            return None

    client.stream = MagicMock(return_value=_StreamCtx())
    backend._client = client
    return backend


def _make_hf_local_backend() -> Any:
    """Build an HFLocalBackend with mocked transformers/torch."""
    # Reuse the same fake-modules strategy as the HF unit tests.
    torch_stub = types.ModuleType("torch")
    torch_stub.bfloat16 = "bfloat16"  # type: ignore[attr-defined]
    torch_stub.float16 = "float16"  # type: ignore[attr-defined]
    torch_stub.no_grad = MagicMock(  # type: ignore[attr-defined]
        return_value=MagicMock(
            __enter__=MagicMock(return_value=None), __exit__=MagicMock(return_value=False)
        )
    )

    tf_stub = types.ModuleType("transformers")

    class FakeTokenizer:
        @classmethod
        def from_pretrained(cls, *_a: Any, **_k: Any) -> FakeTokenizer:
            return cls()

        def __call__(self, text: str, *args: Any, **kwargs: Any) -> Any:
            shape = MagicMock()
            shape.__getitem__ = MagicMock(side_effect=lambda i: 7 if i == 1 else 1)
            ids = MagicMock()
            ids.shape = shape
            ids.to = MagicMock(return_value=ids)
            result = MagicMock()
            result.__getitem__ = MagicMock(return_value=ids)
            result.to = MagicMock(return_value=result)
            return result

        def apply_chat_template(self, messages: Any, **kwargs: Any) -> str:
            return "prompt"

        def decode(self, ids: Any, **kwargs: Any) -> str:
            return "hi"

    class FakeModel:
        device = "cpu"
        generation_config: Any = None

        @classmethod
        def from_pretrained(cls, *_a: Any, **_k: Any) -> FakeModel:
            return cls()

        def generate(self, *args: Any, **kwargs: Any) -> Any:
            shape = MagicMock()
            shape.__getitem__ = MagicMock(return_value=10)
            row = MagicMock()
            row.__getitem__ = MagicMock(return_value=row)
            row.shape = shape
            out = MagicMock()
            out.__getitem__ = MagicMock(return_value=row)
            return out

    class FakeAsyncStreamer:
        def __init__(self, *a: Any, **k: Any) -> None:
            self._items = ["hi"]
            self._i = 0

        def __aiter__(self) -> FakeAsyncStreamer:
            return self

        async def __anext__(self) -> str:
            if self._i >= len(self._items):
                raise StopAsyncIteration
            x = self._items[self._i]
            self._i += 1
            return x

        def end(self) -> None:
            return None

    class FakeStoppingList(list):  # type: ignore[type-arg]
        pass

    class FakeBnB:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

    tf_stub.AutoTokenizer = FakeTokenizer  # type: ignore[attr-defined]
    tf_stub.AutoModelForCausalLM = FakeModel  # type: ignore[attr-defined]
    tf_stub.BitsAndBytesConfig = FakeBnB  # type: ignore[attr-defined]
    tf_stub.GenerationConfig = type("FakeGenConfig", (), {})  # type: ignore[attr-defined]
    tf_stub.StoppingCriteriaList = FakeStoppingList  # type: ignore[attr-defined]
    tf_stub.AsyncTextIteratorStreamer = FakeAsyncStreamer  # type: ignore[attr-defined]

    sys.modules["torch"] = torch_stub
    sys.modules["transformers"] = tf_stub
    from persona.backends.hf_local import HFLocalBackend

    return HFLocalBackend(
        BackendConfig(provider="local", model="local", local_model_id="meta-llama/test")
    )


_BACKENDS: list[tuple[str, str, Callable[[], Any]]] = [
    ("anthropic_via_anthropic_sdk", "anthropic", _make_anthropic_backend),
    ("openai_via_openai_sdk", "openai", _make_openai_backend),
    ("ollama", "ollama", _make_ollama_backend),
    ("hf_local", "local", _make_hf_local_backend),
]


@pytest.fixture(
    params=_BACKENDS,
    ids=[name for name, _, _ in _BACKENDS],
)
def backend_fixture(request: pytest.FixtureRequest) -> Iterator[tuple[Any, str]]:
    _name, expected_provider, factory = request.param
    # The hf_local factory plants torch/transformers STUBS in sys.modules (the
    # backend imports them lazily per call, so they must persist through the
    # test) — but a bare fake torch without ``Tensor`` left behind poisons the
    # first sklearn/scipy import later in the SAME pytest process (scipy's
    # array-api probe getattr(torch, "Tensor") — the R6 crisis-encoder tests
    # were the victims). Save-and-restore around every param.
    saved = {m: sys.modules.get(m) for m in ("torch", "transformers")}
    yield factory(), expected_provider
    for mod, orig in saved.items():
        if orig is not None:
            sys.modules[mod] = orig
        else:
            sys.modules.pop(mod, None)


# -----------------------------------------------------------------------------
# Contract assertions — same for every backend
# -----------------------------------------------------------------------------


class TestProtocolMembership:
    def test_implements_chat_backend(self, backend_fixture: Any) -> None:
        backend, _ = backend_fixture
        assert isinstance(backend, ChatBackend)

    def test_required_properties(self, backend_fixture: Any) -> None:
        backend, expected_provider = backend_fixture
        assert backend.provider_name == expected_provider
        assert isinstance(backend.model_name, str)
        assert backend.model_name
        assert isinstance(backend.supports_native_tools, bool)


class TestChat:
    @pytest.mark.asyncio
    async def test_chat_returns_chat_response(self, backend_fixture: Any) -> None:
        backend, _ = backend_fixture
        response = await backend.chat([_user("hi")])
        assert isinstance(response, ChatResponse)
        assert isinstance(response.content, str)
        assert response.latency_ms >= 0.0

    @pytest.mark.asyncio
    async def test_chat_populates_usage(self, backend_fixture: Any) -> None:
        backend, _ = backend_fixture
        response = await backend.chat([_user("hi")])
        assert response.usage.total_tokens == (
            response.usage.prompt_tokens + response.usage.completion_tokens
        )


class TestStreaming:
    @pytest.mark.asyncio
    async def test_stream_yields_at_least_one_then_final(self, backend_fixture: Any) -> None:
        backend, _ = backend_fixture
        chunks: list[StreamChunk] = []
        async for c in backend.chat_stream([_user("hi")]):
            chunks.append(c)
        assert len(chunks) >= 1
        finals = [c for c in chunks if c.is_final]
        assert len(finals) == 1
        assert finals[0].usage is not None


class TestAuthenticationFailFast:
    """Each backend must raise AuthenticationError when the relevant
    credential is missing at construction time (§10 #8)."""

    def test_openai_compat_missing_api_key_raises(self) -> None:
        config = BackendConfig(provider="openai", model="x", api_key=None)
        with pytest.raises(AuthenticationError):
            OpenAICompatibleBackend(config)

    def test_anthropic_missing_api_key_raises(self) -> None:
        config = BackendConfig(provider="anthropic", model="x", api_key=None)
        with pytest.raises(AuthenticationError):
            OpenAICompatibleBackend(config)

    # Ollama has no required credential, so no "missing key" path.
    # HFLocalBackend without [local] extras raises AuthenticationError —
    # already tested in T09's TestImportGuard.


class TestToolCallRoundTrip:
    @pytest.mark.asyncio
    async def test_tools_argument_accepted(self, backend_fixture: Any) -> None:
        """Passing tools= must not raise. Native or shim — both populate
        ``ChatResponse.tool_calls`` per the Protocol contract."""
        backend, _ = backend_fixture
        tools = [ToolSpec(name="echo", description="echo", parameters={})]
        response = await backend.chat([_user("hi")], tools=tools)
        assert isinstance(response, ChatResponse)
        assert isinstance(response.tool_calls, list)
