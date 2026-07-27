# ruff: noqa: ANN401 — transformers/torch are dynamic imports; Any at boundaries
"""HuggingFace local chat backend.

Wraps a ``transformers.AutoModelForCausalLM`` + ``AutoTokenizer`` behind
the :class:`ChatBackend` Protocol. Lives behind the ``persona-core[local]``
extras (``torch``, ``transformers``, ``bitsandbytes``, ``accelerate``).

Design highlights:

* **Lazy weight load** (D-02-10) — construction validates config + checks
  the optional imports are present, but ``model.generate`` runs on first
  ``chat()``. An ``asyncio.Lock`` guards a one-time load.
* **Async via ``asyncio.to_thread``** — the underlying ``generate`` is
  synchronous; we run it on a worker thread so the event loop stays free.
* **Async streaming** via :class:`transformers.AsyncTextIteratorStreamer`
  (D-02-17) — the older sync streamer + ``to_thread(next, streamer)``
  pattern is a fallback for pre-4.46 transformers.
* **Persona-RAG carry-forwards** (D-02-11): 4-bit NF4 default, 8-bit and
  fp16 supported; eager attention for Gemma-2; warm-up NaN guard
  (external test only); system-role fold for Gemma-2; ``generation_config``
  override to suppress shipped sampling defaults.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import threading
import time
from typing import TYPE_CHECKING, Any

from persona.backends._tool_shim import (
    ShimState,
    new_shim_state,
    parse_tool_call_delta,
    parse_tool_calls,
    render_tool_instructions,
)
from persona.backends.errors import (
    AuthenticationError,
    BackendVisionNotSupportedError,
    ModelNotFoundError,
    ProviderError,
)
from persona.backends.types import (
    ChatResponse,
    StreamChunk,
    TokenUsage,
    ToolSpec,
)
from persona.logging import get_logger
from persona.schema.content import ImageContent

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from persona.backends.config import BackendConfig
    from persona.schema.conversation import ConversationMessage


__all__ = ["HFLocalBackend"]


_LOG = get_logger("backends.hf_local")
_INSTALL_HINT = (
    "HFLocalBackend requires the [local] extras. Run "
    "`uv pip install 'persona-core[local]'` (or `pip install persona-core[local]`)."
)
_GEMMA2_MODEL_HINT = "gemma-2"


class HFLocalBackend:
    """Async chat backend running a local HuggingFace model on GPU.

    Construction is cheap. The first call to :meth:`chat` /
    :meth:`chat_stream` loads weights (~25–35 s for a 9B model at 4-bit
    on M1; less on A100). Subsequent calls reuse the loaded model.
    """

    def __init__(self, config: BackendConfig) -> None:
        if config.provider != "local":
            raise ProviderError(
                f"HFLocalBackend got provider={config.provider!r}",
                context={"provider": config.provider},
            )
        if not config.local_model_id:
            raise ModelNotFoundError(
                "missing PERSONA_LOCAL_MODEL_ID",
                context={"provider": "local"},
            )
        # Check the optional imports are available without actually loading.
        try:
            importlib.import_module("transformers")
            importlib.import_module("torch")
        except ImportError as exc:
            raise AuthenticationError(
                f"{_INSTALL_HINT} (underlying: {exc})",
                context={"provider": "local"},
            ) from exc

        self._config = config
        self._model_id = config.local_model_id
        self._quantization = config.local_quantization
        self._device = config.local_device
        self._load_lock = asyncio.Lock()
        self._model: Any = None
        self._tokenizer: Any = None
        # R9-046: carries the shim's synthetic call_id numbering forward
        # across rounds of one multi-round agentic turn. This instance is
        # cached + reused by the runtime's TierRegistry for the life of the
        # process, so a monotonically-increasing counter here never resets
        # mid-turn (a strict superset of "unique within one turn").
        self._shim_call_seq = 0
        _LOG.debug(
            "constructed",
            provider="local",
            model_id=self._model_id,
            quantization=self._quantization,
            device=self._device,
        )

    @property
    def provider_name(self) -> str:
        return "local"

    @property
    def model_name(self) -> str:
        return self._model_id

    @property
    def supports_native_tools(self) -> bool:
        # Local HF models always use the shim.
        return False

    @property
    def supports_vision(self) -> bool:
        """Always False at v0.1 — HF local is matrix-empty (D-13-3).

        Image-bearing turns raise :class:`BackendVisionNotSupportedError`
        at :meth:`chat` / :meth:`chat_stream` entry before the lazy
        weight load fires. The runtime tier-selector pre-filters around
        this; the in-backend guard is defence in depth.
        """
        return False

    def _guard_vision(self, messages: list[ConversationMessage]) -> None:
        """Fail loud before model load when image content is present.

        Local HF backends do not advertise vision support at v0.1
        (D-13-3). Any :class:`ImageContent` block raises
        :class:`BackendVisionNotSupportedError` with the
        D-13-X-error-hierarchy context shape BEFORE
        :meth:`_ensure_loaded` fires — so the failure mode is loud and
        the runtime never pays for a ~25-35 s 9B-at-4bit model load on
        a routing miss.
        """
        image_count = 0
        for msg in messages:
            if isinstance(msg.content, list):
                image_count += sum(1 for b in msg.content if isinstance(b, ImageContent))
        if image_count:
            raise BackendVisionNotSupportedError(
                "hf_local backend has no vision support at v0.1",
                context={
                    "backend": "hf_local",
                    "model": self._model_id,
                    "image_count": str(image_count),
                },
            )

    # ------------------------------------------------------------------
    # Lazy load
    # ------------------------------------------------------------------

    async def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        async with self._load_lock:
            if self._model is not None:
                return
            await asyncio.to_thread(self._load_blocking)

    def _load_blocking(self) -> None:
        """Synchronous weight load. Called via ``asyncio.to_thread``."""
        transformers = importlib.import_module("transformers")
        torch = importlib.import_module("torch")
        AutoModelForCausalLM = transformers.AutoModelForCausalLM  # noqa: N806
        AutoTokenizer = transformers.AutoTokenizer  # noqa: N806

        quant_config = self._build_quantization_config(transformers, torch)
        load_kwargs: dict[str, Any] = {
            "torch_dtype": (torch.float16 if self._quantization == "none" else torch.bfloat16),
            "device_map": self._device,
        }
        if quant_config is not None:
            load_kwargs["quantization_config"] = quant_config
        # Eager attention for Gemma-2 softcap correctness (D-02-11).
        if _GEMMA2_MODEL_HINT in self._model_id.lower():
            load_kwargs["attn_implementation"] = "eager"

        _LOG.info(
            "loading_model",
            model_id=self._model_id,
            quantization=self._quantization,
            device=self._device,
        )
        self._tokenizer = AutoTokenizer.from_pretrained(self._model_id)
        self._model = AutoModelForCausalLM.from_pretrained(self._model_id, **load_kwargs)
        # Suppress whatever sampling defaults the model card ships with (D-02-11).
        self._model.generation_config = transformers.GenerationConfig()

    def _build_quantization_config(self, transformers: Any, torch: Any) -> Any:
        return _build_quantization_config(transformers, torch, mode=self._quantization)

    # ------------------------------------------------------------------
    # Section: single-shot chat
    # ------------------------------------------------------------------

    async def chat(
        self,
        messages: list[ConversationMessage],
        *,
        tools: list[ToolSpec] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        stop: list[str] | None = None,  # noqa: ARG002 — accepted for protocol parity
        top_p: float | None = None,
        top_k: int | None = None,
    ) -> ChatResponse:
        self._guard_vision(messages)
        await self._ensure_loaded()
        started = time.perf_counter()
        prompt, prompt_tokens = await asyncio.to_thread(self._render_prompt, messages, tools)
        text, completion_tokens = await asyncio.to_thread(
            self._generate_blocking, prompt, temperature, max_tokens, top_p, top_k
        )
        latency_ms = (time.perf_counter() - started) * 1000.0
        # Always shim — strip and parse any tool blocks.
        cleaned, tool_calls = parse_tool_calls(text) if tools else (text, [])
        return ChatResponse(
            content=cleaned,
            tool_calls=tool_calls,
            usage=TokenUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            ),
            model=self._model_id,
            provider="local",
            latency_ms=latency_ms,
        )

    def _generate_blocking(
        self,
        prompt: str,
        temperature: float,
        max_tokens: int,
        top_p: float | None = None,
        top_k: int | None = None,
    ) -> tuple[str, int]:
        assert self._model is not None
        assert self._tokenizer is not None
        torch = importlib.import_module("torch")
        inputs = self._tokenizer(prompt, return_tensors="pt").to(self._model.device)
        do_sample = temperature > 0.0
        gen_kwargs = _sampling_kwargs(do_sample=do_sample, top_p=top_p, top_k=top_k)
        with torch.no_grad():
            output = self._model.generate(
                **inputs,
                max_new_tokens=max_tokens,
                do_sample=do_sample,
                temperature=max(temperature, 1e-5) if do_sample else 1.0,
                **gen_kwargs,
            )
        # Strip prompt tokens from the output to leave only the completion.
        completion_ids = output[0][inputs["input_ids"].shape[1] :]
        text = self._tokenizer.decode(completion_ids, skip_special_tokens=True)
        return text, int(completion_ids.shape[0])

    # ------------------------------------------------------------------
    # Section: streaming chat
    # ------------------------------------------------------------------

    async def chat_stream(
        self,
        messages: list[ConversationMessage],
        *,
        tools: list[ToolSpec] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        stop: list[str] | None = None,  # noqa: ARG002 — accepted for protocol parity
        top_p: float | None = None,
        top_k: int | None = None,
    ) -> AsyncIterator[StreamChunk]:
        self._guard_vision(messages)
        await self._ensure_loaded()
        prompt, prompt_tokens = await asyncio.to_thread(self._render_prompt, messages, tools)
        transformers = importlib.import_module("transformers")
        AsyncTextIteratorStreamer = transformers.AsyncTextIteratorStreamer  # noqa: N806

        streamer = AsyncTextIteratorStreamer(self._tokenizer, skip_prompt=True, timeout=60.0)
        cancel_event = threading.Event()
        thread = threading.Thread(
            target=self._stream_blocking,
            args=(prompt, streamer, temperature, max_tokens, cancel_event, top_p, top_k),
            daemon=True,
        )
        thread.start()

        # R9-046: resume synthetic call_id numbering from the prior round
        # instead of resetting to shim-1 every round.
        shim_state: ShimState | None = (
            new_shim_state(starting_call_seq=self._shim_call_seq) if tools else None
        )
        completion_tokens = 0
        try:
            async for text in streamer:
                if not text:
                    continue
                completion_tokens += 1  # token-rough estimate; final usage corrects
                if shim_state is not None:
                    consumer_text, tc_delta = parse_tool_call_delta(text, shim_state)
                    if consumer_text or tc_delta is not None:
                        yield StreamChunk(delta=consumer_text, tool_call_delta=tc_delta)
                else:
                    yield StreamChunk(delta=text)
        finally:
            cancel_event.set()
            thread.join(timeout=5.0)
            if shim_state is not None:
                # Persist this round's final call_seq so the next round
                # (same backend instance) continues numbering forward.
                self._shim_call_seq = shim_state.call_seq
        yield StreamChunk(
            delta="",
            is_final=True,
            usage=TokenUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            ),
        )

    def _stream_blocking(
        self,
        prompt: str,
        streamer: Any,
        temperature: float,
        max_tokens: int,
        cancel_event: threading.Event,
        top_p: float | None = None,
        top_k: int | None = None,
    ) -> None:
        """Producer thread for streaming. Sets the streamer's end on error."""
        try:
            assert self._model is not None
            assert self._tokenizer is not None
            torch = importlib.import_module("torch")
            transformers = importlib.import_module("transformers")
            inputs = self._tokenizer(prompt, return_tensors="pt").to(self._model.device)
            stopping = transformers.StoppingCriteriaList(
                [_CancellableStoppingCriteria(cancel_event)]
            )
            do_sample = temperature > 0.0
            gen_kwargs = _sampling_kwargs(do_sample=do_sample, top_p=top_p, top_k=top_k)
            with torch.no_grad():
                self._model.generate(
                    **inputs,
                    max_new_tokens=max_tokens,
                    streamer=streamer,
                    do_sample=do_sample,
                    temperature=max(temperature, 1e-5) if do_sample else 1.0,
                    stopping_criteria=stopping,
                    **gen_kwargs,
                )
        except Exception:  # noqa: BLE001 — log + ensure consumer terminates
            _LOG.exception("hf_generate_failed")
        finally:
            # Ensure consumer iteration ends even on producer error.
            if hasattr(streamer, "end"):
                with contextlib.suppress(Exception):
                    streamer.end()

    # ------------------------------------------------------------------
    # Prompt rendering
    # ------------------------------------------------------------------

    def _render_prompt(
        self,
        messages: list[ConversationMessage],
        tools: list[ToolSpec] | None,
    ) -> tuple[str, int]:
        """Render messages to a single text prompt using the tokenizer's chat template."""
        assert self._tokenizer is not None
        msgs = self._fold_system_for_gemma2(messages)
        if tools:
            block = render_tool_instructions(tools)
            if block:
                msgs = self._inject_shim_block(msgs, block)
        chat_messages = [{"role": m["role"], "content": m["content"]} for m in msgs]
        prompt: str = self._tokenizer.apply_chat_template(
            chat_messages, tokenize=False, add_generation_prompt=True
        )
        # Token-count the prompt for usage accounting.
        encoded = self._tokenizer(prompt, return_tensors="pt")
        prompt_tokens = int(encoded["input_ids"].shape[1])
        return prompt, prompt_tokens

    def _fold_system_for_gemma2(self, messages: list[ConversationMessage]) -> list[dict[str, str]]:
        """Fold system messages into the first user message for Gemma-2 (D-02-11).

        Spec 13 T03 widened ``ConversationMessage.content`` to
        ``str | list[MessageContent]``. The hf_local backend does not
        currently advertise vision support, so list-form content here
        is unreachable from the dispatcher — but narrow defensively to
        ``str`` via ``repr()`` so the type checker stays happy and a
        future opt-in path doesn't silently mangle a list payload.
        """

        def _as_str(content: str | object) -> str:
            return content if isinstance(content, str) else repr(content)

        if _GEMMA2_MODEL_HINT not in self._model_id.lower():
            return [{"role": m.role, "content": _as_str(m.content)} for m in messages]
        system_parts: list[str] = []
        rest: list[ConversationMessage] = []
        for m in messages:
            if m.role == "system":
                system_parts.append(_as_str(m.content))
            else:
                rest.append(m)
        if not system_parts:
            return [{"role": m.role, "content": _as_str(m.content)} for m in rest]
        system_text = "\n\n".join(system_parts)
        # Prepend to first user message; if none, create one.
        folded: list[dict[str, str]] = []
        injected = False
        for m in rest:
            if m.role == "user" and not injected:
                folded.append({"role": "user", "content": f"{system_text}\n\n{_as_str(m.content)}"})
                injected = True
            else:
                folded.append({"role": m.role, "content": _as_str(m.content)})
        if not injected:
            folded.insert(0, {"role": "user", "content": system_text})
        return folded

    @staticmethod
    def _inject_shim_block(msgs: list[dict[str, str]], block: str) -> list[dict[str, str]]:
        if msgs and msgs[0]["role"] == "system":
            head = dict(msgs[0])
            head["content"] = f"{head['content']}\n\n{block}"
            return [head, *msgs[1:]]
        return [{"role": "system", "content": block}, *msgs]


def _sampling_kwargs(
    *, do_sample: bool, top_p: float | None, top_k: int | None
) -> dict[str, float | int]:
    """Build the optional ``top_p`` / ``top_k`` kwargs for ``model.generate``.

    Both knobs only affect sampling, so they are emitted only when
    ``do_sample`` is on (temperature > 0). When ``do_sample`` is off, or a knob
    is ``None``, it is omitted entirely — preserving today's greedy-decode
    behaviour byte-for-byte.
    """
    if not do_sample:
        return {}
    kwargs: dict[str, float | int] = {}
    if top_p is not None:
        kwargs["top_p"] = top_p
    if top_k is not None:
        kwargs["top_k"] = top_k
    return kwargs


def _build_quantization_config(transformers: Any, torch: Any, *, mode: str) -> Any:
    """Build a ``BitsAndBytesConfig`` matching the requested quantisation mode."""
    if mode == "none":
        return None
    if not hasattr(transformers, "BitsAndBytesConfig"):
        _LOG.warning("bitsandbytes_unavailable_falling_back_to_fp16")
        return None
    bnb_config = transformers.BitsAndBytesConfig
    if mode == "4bit":
        return bnb_config(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_use_double_quant=True,
            bnb_4bit_compute_dtype=torch.bfloat16,
        )
    if mode == "8bit":
        return bnb_config(load_in_8bit=True)
    return None


class _CancellableStoppingCriteria:
    """``StoppingCriteria`` that stops when a ``threading.Event`` is set.

    Used to terminate the producer thread when the async consumer cancels
    mid-stream (D-02-17 cleanup pattern).
    """

    def __init__(self, event: threading.Event) -> None:
        self._event = event

    def __call__(self, input_ids: Any, scores: Any, **kwargs: Any) -> bool:  # noqa: ANN401, ARG002
        return self._event.is_set()
