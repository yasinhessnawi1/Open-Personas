"""OpenAI-compatible chat backend.

One class that handles every provider exposing an OpenAI-shaped chat API:

* **Anthropic** via the official ``anthropic`` SDK (``AsyncAnthropic``) so we
  keep native Anthropic features (extended thinking, prompt caching) for
  spec 05 to opt into via ``**kwargs``-style pass-through.
* **OpenAI**, **DeepSeek**, **Groq**, **Together** via the official ``openai``
  SDK (``AsyncOpenAI`` with a per-provider ``base_url``).

The class detects the provider at construction, dispatches internally, and
unifies the response shape behind :class:`ChatResponse` / :class:`StreamChunk`.

See ``docs/specs/spec_02/decisions.md`` for the relevant decisions:
* D-02-7 — native-tools capability matrix
* D-02-8 — ``retry_after_s`` extraction from headers
* D-02-13 — two SDKs in core deps
* D-02-18 — OpenAI streaming requires ``stream_options={"include_usage": True}``
"""

from __future__ import annotations

import base64
import json
import time
from typing import TYPE_CHECKING, Any, Final, Literal

import anthropic
import openai

from persona.backends._tool_shim import (
    ShimState,
    parse_tool_call_delta,
    parse_tool_calls,
    render_tool_instructions,
)
from persona.backends.config import DEFAULT_BASE_URLS, BackendConfig
from persona.backends.errors import (
    AuthenticationError,
    BackendTimeoutError,
    BackendVisionNotSupportedError,
    ModelNotFoundError,
    ProviderError,
    RateLimitError,
)
from persona.backends.types import (
    ChatResponse,
    StreamChunk,
    TokenUsage,
    ToolCallDelta,
    ToolSpec,
)
from persona.backends.types import (
    ReasoningBlock as ReasoningBlock,  # re-export hook for downstream typing
)
from persona.logging import get_logger
from persona.schema.content import ImageContent, TextContent
from persona.schema.tools import ToolCall

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from persona.schema.conversation import ConversationMessage


__all__ = ["OpenAICompatibleBackend"]


_LOG = get_logger("backends.openai_compat")


# Per-provider native-tools allow-list (D-02-7). Models not listed silently
# use the prompt-based shim. Default "all" for Anthropic and OpenAI.
_NATIVE_TOOLS_CAPABILITY: dict[str, frozenset[str] | Literal["all"]] = {
    "anthropic": "all",
    "openai": "all",
    "deepseek": frozenset(
        {
            "deepseek-chat",
            "deepseek-v3",
            "deepseek-v3.2",
            "deepseek-v4",
            "deepseek-r1-0528",
        }
    ),
    "groq": frozenset(
        {
            "llama-3.3-70b-versatile",
            "llama-3.1-8b-instant",
            "meta-llama/llama-4-scout-17b-16e-instruct",
            "qwen/qwen3-32b",
        }
    ),
    "together": frozenset(),  # opt-in only; default off
    "nvidia": frozenset(
        {
            "nvidia/llama-3.3-nemotron-super-49b-v1.5",
            "nvidia/nemotron-3-super-120b-a12b",
            "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
        }
    ),
    # Spec 22 D-22-10f: the OpenRouter row ships EMPTY. OpenRouter model
    # strings are full slugs (``anthropic/claude-3.5-sonnet:free``); this row
    # is a pure positive-override hook for operators (an exact-slug entry
    # forces tools=True). Routine capability comes from catalog metadata
    # (tier 2, layered by T10/T11) or underlying-model inference (tier 3,
    # below) — never from pre-seeded entries here that would go stale.
    "openrouter": frozenset(),
    # Cloudflare Workers AI: the OpenAI-compatible endpoint emits native
    # ``tool_calls`` (verified live against ``@cf/zai-org/glm-5.2``). Model ids
    # keep their ``@cf/<vendor>/<model>`` slash-format verbatim; only the
    # function-calling-capable models are listed so others fall back to the
    # prompt shim. Re-verify at deploy as Cloudflare's catalog churns.
    "cloudflare": frozenset(
        {
            "@cf/zai-org/glm-5.2",
            "@cf/meta/llama-4-scout-17b-16e-instruct",
        }
    ),
}


# --- OpenRouter capability inference (Spec 22 D-22-4 + D-22-10) ---
#
# OpenRouter model strings are full slugs: ``author/model[:variant]``. The
# three-tier resolver sits in FRONT of the matrices above:
#   tier 1 — explicit ``openrouter`` row, exact-slug positive override;
#   tier 2 — catalog metadata (``supported_parameters`` / ``input_modalities``),
#            the workhorse, layered in at backend construction by T10/T11;
#   tier 3 — underlying-model inference (this module), the OFFLINE FALLBACK
#            used when the catalog is unavailable or the slug is absent.
# T08 ships tiers 1 + 3 and the reusable helpers; the T10/T11 catalog
# resolver composes tier 1 → tier 2 → tier 3 over these same helpers.

# Author-prefix → existing provider-key map (R-22-4). Only these four author
# prefixes coincide with a populated capability matrix row; all other authors
# (meta-llama, google, mistralai, qwen, ...) have NO row and degrade to the
# conservative shim/no-vision default in tier 3 (the catalog rescues them in
# tier 2 in practice).
_OPENROUTER_AUTHOR_TO_PROVIDER: Final[dict[str, str]] = {
    "anthropic": "anthropic",
    "openai": "openai",
    "deepseek": "deepseek",
    "nvidia": "nvidia",
}

# Documented OpenRouter variant suffixes (R-22-4 / D-22-6). Static variants
# (free/extended/thinking/beta) are separate catalog entries; dynamic variants
# (nitro/floor/exacto/online) are routing transforms. Tier-3 inference strips
# all of them to the base slug; an unknown suffix strips + WARNs (D-22-10d).
_KNOWN_OPENROUTER_VARIANTS: Final[frozenset[str]] = frozenset(
    {"free", "extended", "thinking", "beta", "nitro", "floor", "exacto", "online"}
)


def _matrix_contains(
    matrix: dict[str, frozenset[str] | Literal["all"]], provider: str, name: str
) -> bool:
    """Return whether ``name`` is covered by ``matrix[provider]`` (``"all"`` wins)."""
    capability = matrix.get(provider, frozenset())
    if capability == "all":
        return True
    assert isinstance(capability, frozenset)
    return name in capability


def _explicit_openrouter_entry(
    slug: str, matrix: dict[str, frozenset[str] | Literal["all"]]
) -> bool:
    """Tier 1 — exact-slug positive override in the ``openrouter`` matrix row.

    Returns ``True`` only when an operator has explicitly listed the full
    slug; absence falls through to catalog (tier 2) / inference (tier 3).
    The ``openrouter`` row is never ``"all"`` (D-22-10f), so a plain
    membership test suffices.
    """
    row = matrix.get("openrouter", frozenset())
    assert isinstance(row, frozenset)
    return slug in row


def _infer_openrouter_capability(
    slug: str, *, matrix: dict[str, frozenset[str] | Literal["all"]], is_tools: bool
) -> bool:
    """Tier 3 — underlying-model inference for an OpenRouter slug (R-22-4).

    Conservative by construction (prefers false negatives → prompt shim /
    no image attach), because an optimistic miss surfaces as a hard runtime
    error. Implements: ``:free`` asymmetric conservatism (tools→False per
    verified ``404 No endpoints that support tool use``; vision falls back to
    the base slug — free/paid deltas drop parameters, never input modalities),
    unknown-variant strip+WARN, author-prefix→provider map, and the
    load-bearing DUAL match key (existing rows mix bare names like
    ``deepseek-chat`` and full slugs like ``nvidia/vila``).
    """
    base, _, variant = slug.partition(":")
    author, _, model = base.partition("/")
    if variant == "free" and is_tools:
        return False
    if variant and variant not in _KNOWN_OPENROUTER_VARIANTS:
        _LOG.warning(
            "unknown OpenRouter model variant; stripping to base slug",
            slug=slug,
            variant=variant,
        )
    provider = _OPENROUTER_AUTHOR_TO_PROVIDER.get(author)
    if provider is None:
        # Unmapped author (meta-llama, google, mistralai, ...) → shim default.
        return False
    # Dual match key: try the stripped model name, then the full base slug.
    return _matrix_contains(matrix, provider, model) or _matrix_contains(matrix, provider, base)


def _resolve_openrouter_capability(
    slug: str, *, matrix: dict[str, frozenset[str] | Literal["all"]], is_tools: bool
) -> bool:
    """Resolve an OpenRouter slug's capability via tier 1 → tier 3.

    The catalog tier (tier 2) is composed OVER this function by the T10/T11
    resolver when a catalog is available at construction; at construction
    time without a catalog (Cluster A), capability resolves via the explicit
    override (tier 1) then underlying-model inference (tier 3).
    """
    if _explicit_openrouter_entry(slug, matrix):
        return True
    return _infer_openrouter_capability(slug, matrix=matrix, is_tools=is_tools)


def _native_tools_supported(provider: str, model: str) -> bool:
    """Look up the native-tools capability for a (provider, model) pair."""
    if provider == "openrouter":
        return _resolve_openrouter_capability(model, matrix=_NATIVE_TOOLS_CAPABILITY, is_tools=True)
    return _matrix_contains(_NATIVE_TOOLS_CAPABILITY, provider, model)


# D-13-3 vision capability matrix; verify-at-deploy per T19 close-out.
# D-20-1 (Spec 20): NVIDIA vision tier defaults to VILA / Cosmos (NVIDIA Open
# Model License — no EU carve-out, no anti-distillation) over Llama-3.2-Vision
# (Llama 3.2 Community License §1(a) excludes EU-domiciled developers per R-20-5).
_VISION_CAPABILITY: dict[str, frozenset[str] | Literal["all"]] = {
    "anthropic": "all",
    "openai": frozenset({"gpt-4o", "gpt-4o-mini", "gpt-4-turbo"}),
    "deepseek": frozenset(),
    "groq": frozenset(),
    "together": frozenset(),
    "nvidia": frozenset(
        {
            # T09 entry — omni-modal Nemotron (text/image/video/speech in).
            "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
            # T13 entries — NVIDIA-owned VLMs verified at build.nvidia.com
            # (Open Model License; no EU carve-out per R-20-5 + D-20-1).
            # VILA family — https://build.nvidia.com/nvidia/vila
            "nvidia/vila",
            # Cosmos Nemotron VLM (VILA's successor per NVIDIA Jan-2025
            # rebranding) — https://build.nvidia.com/nvidia/cosmos-nemotron-34b
            "nvidia/cosmos-nemotron-34b",
            # Cosmos Reason vision-reasoning VLMs for physical-AI workloads.
            # https://build.nvidia.com/nvidia/cosmos-reason1-7b
            # https://build.nvidia.com/nvidia/cosmos-reason2-8b
            "nvidia/cosmos-reason1-7b",
            "nvidia/cosmos-reason2-8b",
        }
    ),
    # Spec 22 D-22-10f: empty OpenRouter override row (see the tools matrix
    # note above). Vision capability resolves via catalog ``input_modalities``
    # (tier 2) or underlying-model inference (tier 3); vision inference for a
    # ``:free`` slug falls back to the base slug (D-22-10c).
    "openrouter": frozenset(),
    # Cloudflare Workers AI multimodal (image-in) chat models. Model ids keep
    # their ``@cf/<vendor>/<model>`` slash-format verbatim. Verify-at-deploy
    # like the NVIDIA row above — Cloudflare's model catalog evolves.
    "cloudflare": frozenset(
        {
            "@cf/zai-org/glm-5.2",
            "@cf/meta/llama-4-scout-17b-16e-instruct",
        }
    ),
}

# D-13-3 "verify-at-deploy" precedent — model IDs above were sourced from a
# build.nvidia.com catalog scan at T13 implementation time. Exact slugs are
# subject to NVIDIA-side rename (the VILA → Cosmos Nemotron consolidation
# announced January 2025 is the most recent example). Operators MUST re-verify
# the four IDs in this constant at deploy time; T25 MAINTENANCE.md row tracks
# the operator re-verification cadence per the D-20-7 event-driven trigger.
_NVIDIA_VISION_MODELS_VERIFY_AT_DEPLOY: Final[frozenset[str]] = frozenset(
    {
        "nvidia/vila",
        "nvidia/cosmos-nemotron-34b",
        "nvidia/cosmos-reason1-7b",
        "nvidia/cosmos-reason2-8b",
    }
)


def vision_supported(provider: str, model: str) -> bool:
    """Look up the vision capability for a (provider, model) pair (D-13-3).

    Public, static, key-free: the same O(1) ``_VISION_CAPABILITY`` lookup the
    :class:`OpenAICompatibleBackend` constructor uses to set its own
    ``supports_vision`` (R1-D-1a). Exposing one function keeps a single source
    of truth — the runtime tier registry reads vision capability without
    instantiating a backend (no API key required), and the router pre-filter
    can never disagree with what the backend enforces at the boundary.
    """
    if provider == "openrouter":
        return _resolve_openrouter_capability(model, matrix=_VISION_CAPABILITY, is_tools=False)
    return _matrix_contains(_VISION_CAPABILITY, provider, model)


def _extract_retry_after_s(headers: Any) -> str | None:  # noqa: ANN401 — SDK type
    """Return ``retry-after`` header value as a string, or None (D-02-8)."""
    if headers is None:
        return None
    try:
        value = headers.get("retry-after") if hasattr(headers, "get") else None
    except (AttributeError, TypeError):
        return None
    if value is None:
        return None
    return str(value)


class OpenAICompatibleBackend:
    """Async chat backend for every OpenAI-compatible provider.

    Internally dispatches between ``anthropic.AsyncAnthropic`` (for
    ``provider="anthropic"``) and ``openai.AsyncOpenAI`` (for everything
    else). The dispatch is contained — callers see one shape.
    """

    def __init__(self, config: BackendConfig, *, workspace_root: Path | None = None) -> None:
        """Construct and validate. Raises :class:`AuthenticationError` on
        missing key (D-02-13 + spec §10 #8).

        Args:
            config: Backend configuration. ``provider`` must be one of
                ``anthropic | openai | deepseek | groq | together | nvidia |
                openrouter`` (Spec 20 added nvidia per
                D-20-X-nvidia-allow-set-extend; Spec 22 added openrouter per
                the same allow-set-extend invariant — adding to the Provider
                Literal alone is necessary but NOT sufficient).
            workspace_root: Optional persona workspace root used by the
                Spec 13 multimodal serialisers (T05/T06) to resolve
                :class:`ImageContent` workspace-path refs to bytes. Most
                callers leave this ``None``; only the persona-api
                composition root supplies it. When ``None`` and a
                list-form message carries an :class:`ImageContent`
                block, :class:`BackendVisionNotSupportedError` is
                raised at serialisation time so the failure mode is
                loud rather than a silent text-only round-trip.
        """
        if config.provider not in {
            "anthropic",
            "openai",
            "deepseek",
            "groq",
            "together",
            "nvidia",
            "openrouter",
            "cloudflare",
        }:
            msg = (
                f"OpenAICompatibleBackend does not handle provider "
                f"{config.provider!r}; use load_backend() to dispatch."
            )
            raise ProviderError(msg, context={"provider": config.provider})

        if config.api_key is None or not config.api_key.get_secret_value():
            raise AuthenticationError("missing API key", context={"provider": config.provider})

        self._config = config
        self._provider = config.provider
        self._model = config.model
        self._timeout = config.request_timeout_s
        self._supports_native_tools = _native_tools_supported(self._provider, self._model)
        self._supports_vision = vision_supported(self._provider, self._model)
        self._workspace_root = workspace_root

        api_key = config.api_key.get_secret_value()
        base_url = config.base_url or DEFAULT_BASE_URLS.get(self._provider)

        if self._provider == "anthropic":
            self._anthropic = anthropic.AsyncAnthropic(
                api_key=api_key, base_url=base_url, timeout=self._timeout
            )
            self._openai: openai.AsyncOpenAI | None = None
        else:
            self._anthropic = None  # type: ignore[assignment]
            self._openai = openai.AsyncOpenAI(
                api_key=api_key, base_url=base_url, timeout=self._timeout
            )

        _LOG.debug(
            "constructed",
            provider=self._provider,
            model=self._model,
            native_tools=self._supports_native_tools,
            vision=self._supports_vision,
        )

    @property
    def provider_name(self) -> str:
        return self._provider

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def supports_native_tools(self) -> bool:
        return self._supports_native_tools

    @property
    def supports_vision(self) -> bool:
        return self._supports_vision

    async def chat(
        self,
        messages: list[ConversationMessage],
        *,
        tools: list[ToolSpec] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        stop: list[str] | None = None,
        top_p: float | None = None,
        top_k: int | None = None,
    ) -> ChatResponse:
        """Single-shot chat. See ``ChatBackend.chat`` for contract.

        ``top_p`` is forwarded to both the Anthropic and OpenAI SDKs when set.
        ``top_k`` is Anthropic-only: it is forwarded on the Anthropic path and
        is a documented NO-OP on the OpenAI path (the OpenAI Chat Completions
        API has no ``top_k`` parameter).
        """
        started = time.perf_counter()
        try:
            if self._provider == "anthropic":
                response = await self._chat_anthropic(
                    messages, tools, temperature, max_tokens, stop, top_p, top_k
                )
            else:
                response = await self._chat_openai(
                    messages, tools, temperature, max_tokens, stop, top_p
                )
        except Exception as exc:
            self._reraise(exc)

        latency_ms = (time.perf_counter() - started) * 1000.0
        return response.model_copy(update={"latency_ms": latency_ms})

    async def chat_stream(
        self,
        messages: list[ConversationMessage],
        *,
        tools: list[ToolSpec] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        stop: list[str] | None = None,
        top_p: float | None = None,
        top_k: int | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """Streaming chat. See ``ChatBackend.chat_stream`` for contract.

        ``top_p`` / ``top_k`` follow the same provider-support rules as
        :meth:`chat` (``top_k`` is a NO-OP on the OpenAI path).
        """
        try:
            if self._provider == "anthropic":
                async for chunk in self._stream_anthropic(
                    messages, tools, temperature, max_tokens, stop, top_p, top_k
                ):
                    yield chunk
            else:
                async for chunk in self._stream_openai(
                    messages, tools, temperature, max_tokens, stop, top_p
                ):
                    yield chunk
        except Exception as exc:
            self._reraise(exc)

    # ------------------------------------------------------------------
    # Anthropic dispatch
    # ------------------------------------------------------------------

    async def _chat_anthropic(
        self,
        messages: list[ConversationMessage],
        tools: list[ToolSpec] | None,
        temperature: float,
        max_tokens: int,
        stop: list[str] | None,
        top_p: float | None = None,
        top_k: int | None = None,
    ) -> ChatResponse:
        assert self._anthropic is not None
        use_native = self._supports_native_tools and bool(tools)
        system_text, msgs = _split_system(messages)
        if not use_native and tools:
            system_text = _append_shim_instructions(system_text, tools)

        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": _coalesce_anthropic_tool_results(
                [
                    _message_to_anthropic(
                        m,
                        workspace_root=self._workspace_root,
                        supports_vision=self._supports_vision,
                        backend=self._provider,
                        model=self._model,
                    )
                    for m in msgs
                ]
            ),
            "temperature": temperature,
        }
        if system_text:
            kwargs["system"] = system_text
        if stop:
            kwargs["stop_sequences"] = stop
        # Anthropic supports both nucleus (top_p) and top_k sampling.
        if top_p is not None:
            kwargs["top_p"] = top_p
        if top_k is not None:
            kwargs["top_k"] = top_k
        if use_native and tools:
            kwargs["tools"] = [_tool_spec_to_anthropic(t) for t in tools]

        response = await self._anthropic.messages.create(**kwargs)
        return _parse_anthropic_response(response, self._provider, use_native)

    async def _stream_anthropic(
        self,
        messages: list[ConversationMessage],
        tools: list[ToolSpec] | None,
        temperature: float,
        max_tokens: int,
        stop: list[str] | None,
        top_p: float | None = None,
        top_k: int | None = None,
    ) -> AsyncIterator[StreamChunk]:
        assert self._anthropic is not None
        use_native = self._supports_native_tools and bool(tools)
        system_text, msgs = _split_system(messages)
        shim_state: ShimState | None = None
        if not use_native and tools:
            system_text = _append_shim_instructions(system_text, tools)
            shim_state = ShimState()

        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": _coalesce_anthropic_tool_results(
                [
                    _message_to_anthropic(
                        m,
                        workspace_root=self._workspace_root,
                        supports_vision=self._supports_vision,
                        backend=self._provider,
                        model=self._model,
                    )
                    for m in msgs
                ]
            ),
            "temperature": temperature,
        }
        if system_text:
            kwargs["system"] = system_text
        if stop:
            kwargs["stop_sequences"] = stop
        # Anthropic supports both nucleus (top_p) and top_k sampling.
        if top_p is not None:
            kwargs["top_p"] = top_p
        if top_k is not None:
            kwargs["top_k"] = top_k
        if use_native and tools:
            kwargs["tools"] = [_tool_spec_to_anthropic(t) for t in tools]

        usage: TokenUsage | None = None
        prompt_tokens = 0
        completion_tokens = 0
        # Anthropic streaming protocol — `content_block_start` carries the
        # tool_use's real id + name once; subsequent `content_block_delta`
        # `input_json_delta` events carry only the *block index* and partial
        # JSON. We must thread the real id forward so the runtime loop's
        # accumulator keys the call by its real Anthropic id (toolu_…) — not
        # the block index — otherwise the assistant.tool_calls payload on the
        # round-2 re-prompt has id="0" + name="" and Anthropic 400s with
        # `tool_use.name: String should have at least 1 character`.
        # Spec 11 launch finding — mirrors the OpenAI `id_by_index` fix.
        id_by_index: dict[int, str] = {}

        async with self._anthropic.messages.stream(**kwargs) as stream:
            async for event in stream:
                event_type = getattr(event, "type", "")
                if event_type == "content_block_delta":
                    delta = getattr(event, "delta", None)
                    delta_type = getattr(delta, "type", "")
                    if delta_type == "text_delta":
                        text = getattr(delta, "text", "")
                        if shim_state is not None:
                            consumer_text, tc_delta = parse_tool_call_delta(text, shim_state)
                            if consumer_text or tc_delta is not None:
                                yield StreamChunk(
                                    delta=consumer_text,
                                    tool_call_delta=tc_delta,
                                )
                        elif text:
                            yield StreamChunk(delta=text)
                    elif delta_type == "input_json_delta":
                        partial = getattr(delta, "partial_json", "")
                        idx = getattr(event, "index", 0) or 0
                        # Resolve the real Anthropic id captured at content_block_start.
                        # Fall back to a synthesised one only if the provider skipped
                        # the start event entirely (defensive).
                        resolved_id = id_by_index.get(idx, f"toolu_idx_{idx}")
                        yield StreamChunk(
                            delta="",
                            tool_call_delta=ToolCallDelta(
                                call_id=resolved_id,
                                arguments_delta=partial,
                            ),
                        )
                elif event_type == "content_block_start":
                    block = getattr(event, "content_block", None)
                    if getattr(block, "type", "") == "tool_use":
                        idx = getattr(event, "index", 0) or 0
                        real_id = getattr(block, "id", "") or f"toolu_idx_{idx}"
                        name = getattr(block, "name", "") or ""
                        id_by_index[idx] = real_id
                        # Emit the id + name once so the runtime loop's
                        # accumulator records them on the first sighting; later
                        # input_json_delta events thread the same id forward.
                        yield StreamChunk(
                            delta="",
                            tool_call_delta=ToolCallDelta(
                                call_id=real_id,
                                name_delta=name,
                                arguments_delta="",
                            ),
                        )
                elif event_type == "message_delta":
                    usage_obj = getattr(event, "usage", None)
                    if usage_obj is not None:
                        completion_tokens = (
                            getattr(usage_obj, "output_tokens", completion_tokens)
                            or completion_tokens
                        )

            # Stream consumed; collect final usage from accumulator.
            final_message = await stream.get_final_message()
            usage_obj = getattr(final_message, "usage", None)
            if usage_obj is not None:
                prompt_tokens = getattr(usage_obj, "input_tokens", 0) or 0
                completion_tokens = (
                    getattr(usage_obj, "output_tokens", completion_tokens) or completion_tokens
                )

        usage = TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        )
        yield StreamChunk(delta="", is_final=True, usage=usage)

    # ------------------------------------------------------------------
    # OpenAI / DeepSeek / Groq / Together dispatch
    # ------------------------------------------------------------------

    async def _chat_openai(
        self,
        messages: list[ConversationMessage],
        tools: list[ToolSpec] | None,
        temperature: float,
        max_tokens: int,
        stop: list[str] | None,
        top_p: float | None = None,
    ) -> ChatResponse:
        # NB: ``top_k`` is intentionally NOT a parameter here — the OpenAI Chat
        # Completions API has no ``top_k`` knob. The public ``chat`` entrypoint
        # drops it on this path (documented NO-OP); only ``top_p`` is forwarded.
        assert self._openai is not None
        use_native = self._supports_native_tools and bool(tools)
        msgs = [
            _message_to_openai(
                m,
                workspace_root=self._workspace_root,
                supports_vision=self._supports_vision,
                backend=self._provider,
                model=self._model,
            )
            for m in messages
        ]
        if not use_native and tools:
            msgs = _prepend_shim_to_openai(msgs, tools)

        # D-20-X-deepseek-reasoning-strip-invariant: DeepSeek returns HTTP 400
        # if ``reasoning_content`` is echoed in input messages. Strip from prior
        # assistant turns before sending. Runtime currently authors assistant
        # text as a plain string (no reasoning field on the wire), so this is
        # a defensive invariant guarding future serialisers that lift reasoning
        # into the message payload.
        msgs = _strip_reasoning_for_provider(msgs, self._provider)

        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": msgs,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if stop:
            kwargs["stop"] = stop
        if top_p is not None:
            kwargs["top_p"] = top_p
        if use_native and tools:
            # R4-C1-13: cover history tool_calls so a strict provider (groq) does
            # not 400 on a hallucinated call the model already got an error for.
            kwargs["tools"] = _openai_tools_covering_history(tools, messages)
        # D-20-3: opaque pass-through to the vendor SDK's ``extra_body``.
        if self._config.extra_body is not None:
            kwargs["extra_body"] = self._config.extra_body

        response = await self._openai.chat.completions.create(**kwargs)
        return _parse_openai_response(response, self._provider, use_native)

    async def _stream_openai(
        self,
        messages: list[ConversationMessage],
        tools: list[ToolSpec] | None,
        temperature: float,
        max_tokens: int,
        stop: list[str] | None,
        top_p: float | None = None,
    ) -> AsyncIterator[StreamChunk]:
        # ``top_k`` is NOT accepted here (no OpenAI parameter); see _chat_openai.
        assert self._openai is not None
        use_native = self._supports_native_tools and bool(tools)
        msgs = [
            _message_to_openai(
                m,
                workspace_root=self._workspace_root,
                supports_vision=self._supports_vision,
                backend=self._provider,
                model=self._model,
            )
            for m in messages
        ]
        shim_state: ShimState | None = None
        if not use_native and tools:
            msgs = _prepend_shim_to_openai(msgs, tools)
            shim_state = ShimState()

        # D-20-X-deepseek-reasoning-strip-invariant (mirrors _chat_openai).
        msgs = _strip_reasoning_for_provider(msgs, self._provider)

        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": msgs,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},  # D-02-18
        }
        if stop:
            kwargs["stop"] = stop
        if top_p is not None:
            kwargs["top_p"] = top_p
        if use_native and tools:
            # R4-C1-13: cover history tool_calls so a strict provider (groq) does
            # not 400 on a hallucinated call the model already got an error for.
            kwargs["tools"] = _openai_tools_covering_history(tools, messages)
        # D-20-3: opaque pass-through to the vendor SDK's ``extra_body``.
        if self._config.extra_body is not None:
            kwargs["extra_body"] = self._config.extra_body

        usage: TokenUsage | None = None
        stream = await self._openai.chat.completions.create(**kwargs)
        # OpenAI/DeepSeek streaming sends a tool call's `id` only on its FIRST
        # delta; continuation deltas carry the same `index` but an empty `id`.
        # Resolve the stable id by index so every emitted delta carries it (spec
        # 11 soak finding — otherwise continuations collapse to call_id="").
        id_by_index: dict[int, str] = {}
        async for chunk in stream:
            chunk_usage = getattr(chunk, "usage", None)
            if chunk_usage is not None:
                usage = TokenUsage(
                    prompt_tokens=chunk_usage.prompt_tokens or 0,
                    completion_tokens=chunk_usage.completion_tokens or 0,
                    total_tokens=(chunk_usage.prompt_tokens or 0)
                    + (chunk_usage.completion_tokens or 0),
                )
            choices = getattr(chunk, "choices", []) or []
            if not choices:
                continue
            delta = getattr(choices[0], "delta", None)
            text = getattr(delta, "content", None) or ""
            tool_calls = getattr(delta, "tool_calls", None) or []
            # D-20-X-nemotron-field-name-dual-probe: NVIDIA Nemotron emits via
            # two field names depending on model family — canonical
            # ``reasoning_content`` OR alias ``reasoning`` on newer Nano-Omni
            # VLM endpoints. Both arrive via Pydantic extras on openai-py
            # ``ChoiceDelta`` (NOT statically typed). Probe both. DeepSeek-R1
            # and OpenAI Chat Completions also fit the str arm.
            reasoning_delta = getattr(delta, "reasoning_content", None) or getattr(
                delta, "reasoning", None
            )
            if text:
                if shim_state is not None:
                    consumer_text, tc_delta = parse_tool_call_delta(text, shim_state)
                    if consumer_text or tc_delta is not None:
                        yield StreamChunk(
                            delta=consumer_text,
                            tool_call_delta=tc_delta,
                            reasoning=reasoning_delta if reasoning_delta else None,
                        )
                        reasoning_delta = None  # consumed
                else:
                    yield StreamChunk(
                        delta=text,
                        reasoning=reasoning_delta if reasoning_delta else None,
                    )
                    reasoning_delta = None  # consumed
            elif reasoning_delta:
                # Reasoning-only chunk (no text delta): emit a StreamChunk
                # carrying only the reasoning fragment so the runtime can
                # buffer it for the TurnLog hash.
                yield StreamChunk(delta="", reasoning=reasoning_delta)
            for tc in tool_calls:
                fn = getattr(tc, "function", None)
                idx = getattr(tc, "index", 0) or 0
                raw_id = getattr(tc, "id", "") or ""
                if raw_id:
                    id_by_index[idx] = raw_id
                elif idx not in id_by_index:
                    # Some providers (DeepSeek) stream tool calls with an `index`
                    # but no `id`; synthesise a stable, unique id per index so the
                    # call's deltas accumulate together and the assistant.tool_calls
                    # id matches its tool_result tool_call_id (spec 11 soak finding).
                    id_by_index[idx] = f"call_{idx}"
                yield StreamChunk(
                    delta="",
                    tool_call_delta=ToolCallDelta(
                        call_id=id_by_index[idx],
                        name_delta=(getattr(fn, "name", "") if fn else "") or "",
                        arguments_delta=(getattr(fn, "arguments", "") if fn else "") or "",
                    ),
                )

        if usage is None:
            usage = TokenUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0)
        yield StreamChunk(delta="", is_final=True, usage=usage)

    # ------------------------------------------------------------------
    # Error mapping
    # ------------------------------------------------------------------

    def _reraise(self, exc: BaseException) -> Any:  # noqa: ANN401 — re-raises
        """Map a provider SDK exception to a domain exception and re-raise."""
        provider = self._provider
        model = self._model
        # Anthropic SDK
        if isinstance(exc, anthropic.AuthenticationError):
            raise AuthenticationError(str(exc), context={"provider": provider}) from exc
        if isinstance(exc, anthropic.RateLimitError):
            retry_after = _extract_retry_after_s(
                getattr(getattr(exc, "response", None), "headers", None)
            )
            ctx: dict[str, str] = {"provider": provider}
            if retry_after is not None:
                ctx["retry_after_s"] = retry_after
            raise RateLimitError(str(exc), context=ctx) from exc
        if isinstance(exc, anthropic.NotFoundError):
            raise ModelNotFoundError(
                str(exc), context={"provider": provider, "model": model}
            ) from exc
        if isinstance(exc, anthropic.APITimeoutError | anthropic.APIConnectionError):
            raise BackendTimeoutError(str(exc), context={"provider": provider}) from exc

        # OpenAI SDK
        if isinstance(exc, openai.AuthenticationError):
            raise AuthenticationError(str(exc), context={"provider": provider}) from exc
        if isinstance(exc, openai.RateLimitError):
            retry_after = _extract_retry_after_s(
                getattr(getattr(exc, "response", None), "headers", None)
            )
            openai_ctx: dict[str, str] = {"provider": provider}
            if retry_after is not None:
                openai_ctx["retry_after_s"] = retry_after
            raise RateLimitError(str(exc), context=openai_ctx) from exc
        if isinstance(exc, openai.NotFoundError):
            raise ModelNotFoundError(
                str(exc), context={"provider": provider, "model": model}
            ) from exc
        if isinstance(exc, openai.APITimeoutError | openai.APIConnectionError):
            raise BackendTimeoutError(str(exc), context={"provider": provider}) from exc

        # Anything else
        raise ProviderError(
            str(exc),
            context={"provider": provider, "underlying": type(exc).__name__},
        ) from exc


# ----------------------------------------------------------------------
# Message + tool conversion helpers
# ----------------------------------------------------------------------


def _split_system(
    messages: list[ConversationMessage],
) -> tuple[str, list[ConversationMessage]]:
    """Pull out system messages (Anthropic wants them as a top-level field).

    Spec 13 T03 widened ``ConversationMessage.content`` to
    ``str | list[MessageContent]``. System messages are authored by the
    runtime as plain str (per the T01 audit's "preserved text-only"
    classification), so we narrow defensively to str here — any
    list-form content is refused upstream by the vision dispatcher
    introduced in T05/T06.
    """
    system_parts = [
        m.content for m in messages if m.role == "system" and isinstance(m.content, str)
    ]
    rest = [m for m in messages if m.role != "system"]
    return "\n\n".join(system_parts), rest


def _append_shim_instructions(system_text: str, tools: list[ToolSpec]) -> str:
    block = render_tool_instructions(tools)
    if not block:
        return system_text
    return f"{system_text}\n\n{block}".strip()


def _coalesce_anthropic_tool_results(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge consecutive user messages whose content is a list of blocks.

    Anthropic's tool-protocol invariant: every ``tool_use`` block in an assistant
    message MUST have a matching ``tool_result`` block in the **single** user
    message immediately after — Anthropic 400s with
    ``messages.N: tool_use ids were found without tool_result blocks immediately
    after`` if the results are spread across two consecutive user messages.

    The conversation loop appends one ``role="tool"`` ConversationMessage per
    dispatched call, and :func:`_message_to_anthropic` lifts each into its own
    user message with a single ``tool_result`` block. For N>=2 simultaneous tool
    calls we end up with N consecutive user messages on the wire. We coalesce
    them here at the backend boundary so the loop and the schema stay simple.

    Spec 11 launch finding — only matters for native Anthropic tool calling.
    """
    coalesced: list[dict[str, Any]] = []
    for msg in messages:
        if (
            coalesced
            and coalesced[-1].get("role") == "user"
            and isinstance(coalesced[-1].get("content"), list)
            and msg.get("role") == "user"
            and isinstance(msg.get("content"), list)
        ):
            # Both prev and current carry block-list content; merge them.
            coalesced[-1] = {
                **coalesced[-1],
                "content": [*coalesced[-1]["content"], *msg["content"]],
            }
        else:
            coalesced.append(msg)
    return coalesced


def _resolve_image_bytes(block: ImageContent, workspace_root: Path | None) -> bytes:
    """Resolve one :class:`ImageContent` block to its raw bytes.

    Prefers already-resolved ``inline_bytes`` (the live image-workspace cascade:
    the chat tier backend is app-scoped/cached and has no per-request
    ``workspace_root``, so the bytes ride on the block itself). Falls back to
    reading ``workspace_root / block.workspace_path`` from disk for the
    persisted-history replay path. The serialiser guards already guarantee a
    non-``None`` ``workspace_root`` for any block lacking inline bytes, so the
    disk read is reached only when it is safe.

    Args:
        block: The image block to resolve.
        workspace_root: The configured workspace root, or ``None``.

    Returns:
        The raw image bytes.
    """
    if block.inline_bytes is not None:
        return block.inline_bytes
    assert workspace_root is not None  # narrowed by the caller's guard
    return (workspace_root / block.workspace_path).read_bytes()


def _message_to_anthropic(
    msg: ConversationMessage,
    *,
    workspace_root: Path | None = None,
    supports_vision: bool = True,
    backend: str = "",
    model: str = "",
) -> dict[str, Any]:
    """Convert one ``ConversationMessage`` to Anthropic's message shape.

    Anthropic accepts ``user`` and ``assistant`` roles only at the message
    level (system goes via the top-level ``system`` field). ``tool``
    messages from spec 01's schema are folded into a user message carrying
    a tool_result block.

    Spec 13 T05 widens ``msg.content`` to ``str | list[MessageContent]``.
    The str path is byte-for-byte unchanged (the T01 snapshot corpus
    gates this). For the list path, :class:`TextContent` blocks become
    ``{"type": "text", "text": ...}`` and :class:`ImageContent` blocks
    become ``{"type": "image", "source": {"type": "base64",
    "media_type": ..., "data": ...}}`` with bytes read from
    ``workspace_root / block.workspace_path`` and base64-encoded (per
    D-13-2). If ``supports_vision`` is ``False`` or ``workspace_root``
    is ``None`` and a list contains any :class:`ImageContent`, this
    raises :class:`BackendVisionNotSupportedError` BEFORE touching the
    filesystem so the failure mode is loud and synchronous.
    """
    role = msg.role
    if role == "tool":
        # tool messages are authored by the runtime as plain str; the schema
        # widening to list never reaches here, but narrow defensively so the
        # tool_result block carries a str content (matching the existing wire
        # shape gated by spec 11 launch tests).
        tool_content = msg.content if isinstance(msg.content, str) else ""
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": msg.metadata.get("tool_call_id", ""),
                    "content": tool_content,
                }
            ],
        }
    if role == "assistant" and msg.tool_calls:
        # Anthropic requires the assistant's tool_use blocks to precede the
        # matching tool_result (spec 11 soak finding). An optional leading text
        # block carries any narration. Assistant messages with tool_calls are
        # authored by the model + lifted by the runtime as text-only str.
        blocks: list[dict[str, Any]] = []
        if msg.content and isinstance(msg.content, str):
            blocks.append({"type": "text", "text": msg.content})
        blocks.extend(
            {"type": "tool_use", "id": tc.call_id, "name": tc.name, "input": tc.args}
            for tc in msg.tool_calls
        )
        return {"role": "assistant", "content": blocks}
    if isinstance(msg.content, list):
        # Multimodal list form (T05). Fail fast if vision is not configured
        # (supports_vision=False or no workspace_root supplied) before any
        # filesystem touch.
        image_count = sum(1 for b in msg.content if isinstance(b, ImageContent))
        if image_count and not supports_vision:
            raise BackendVisionNotSupportedError(
                "backend does not support vision",
                context={
                    "backend": backend,
                    "model": model,
                    "image_count": str(image_count),
                },
            )
        # Only images WITHOUT already-resolved inline bytes need a
        # ``workspace_root`` to read from disk. Live-cascade images carry their
        # bytes inline (the app-scoped/cached chat backend has no per-request
        # ``workspace_root``), so they serialise without one.
        needs_root = sum(
            1 for b in msg.content if isinstance(b, ImageContent) and b.inline_bytes is None
        )
        if needs_root and workspace_root is None:
            raise BackendVisionNotSupportedError(
                "no workspace_root configured for image resolution",
                context={
                    "backend": backend,
                    "model": model,
                    "image_count": str(needs_root),
                    "reason": "missing_workspace_root",
                },
            )
        out_blocks: list[dict[str, Any]] = []
        for block in msg.content:
            if isinstance(block, TextContent):
                out_blocks.append({"type": "text", "text": block.text})
            elif isinstance(block, ImageContent):
                image_bytes = _resolve_image_bytes(block, workspace_root)
                data_b64 = base64.standard_b64encode(image_bytes).decode("ascii")
                out_blocks.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": block.media_type,
                            "data": data_b64,
                        },
                    }
                )
        return {"role": role, "content": out_blocks}
    return {"role": role, "content": msg.content}


def _message_to_openai(
    msg: ConversationMessage,
    *,
    workspace_root: Path | None = None,
    supports_vision: bool = True,
    backend: str = "",
    model: str = "",
) -> dict[str, Any]:
    """Convert one ``ConversationMessage`` to OpenAI's message shape.

    Spec 13 T06 widens this serialiser to handle the
    ``str | list[MessageContent]`` content type introduced by T03. The
    str path is byte-for-byte unchanged. For the list path, blocks are
    emitted as an OpenAI multi-part content array (per the OpenAI
    Vision Chat Completions schema):

    * :class:`TextContent` -> ``{"type": "text", "text": ...}``.
    * :class:`ImageContent` -> ``{"type": "image_url",
      "image_url": {"url": "data:<media_type>;base64,<b64>"}}`` per
      D-13-2 (Anthropic only accepts base64, and the same data-URL
      payload works for OpenAI's vision models — keeps both wire shapes
      symmetric and avoids a second persisted form).

    If ``supports_vision`` is ``False`` or ``workspace_root`` is
    ``None`` and the list carries any :class:`ImageContent`, this
    raises :class:`BackendVisionNotSupportedError` BEFORE touching the
    filesystem so the failure mode is loud and synchronous (same guard
    semantics as :func:`_message_to_anthropic`).

    Tool messages and assistant.tool_calls follow the str path —
    ``role="tool"`` carries a single str body (the runtime never lifts
    a tool result into the list form) and ``assistant.tool_calls``
    flows through the existing OpenAI function-call schema.
    """
    if isinstance(msg.content, list):
        # Multimodal list form (T06). Fail fast if vision is not configured
        # (supports_vision=False or no workspace_root supplied) before any
        # filesystem touch.
        image_count = sum(1 for b in msg.content if isinstance(b, ImageContent))
        if image_count and not supports_vision:
            raise BackendVisionNotSupportedError(
                "backend does not support vision",
                context={
                    "backend": backend,
                    "model": model,
                    "image_count": str(image_count),
                },
            )
        # Only images WITHOUT already-resolved inline bytes need a
        # ``workspace_root`` to read from disk (see :func:`_message_to_anthropic`).
        needs_root = sum(
            1 for b in msg.content if isinstance(b, ImageContent) and b.inline_bytes is None
        )
        if needs_root and workspace_root is None:
            raise BackendVisionNotSupportedError(
                "no workspace_root configured for image resolution",
                context={
                    "backend": backend,
                    "model": model,
                    "image_count": str(needs_root),
                    "reason": "missing_workspace_root",
                },
            )
        out_parts: list[dict[str, Any]] = []
        for block in msg.content:
            if isinstance(block, TextContent):
                out_parts.append({"type": "text", "text": block.text})
            elif isinstance(block, ImageContent):
                image_bytes = _resolve_image_bytes(block, workspace_root)
                data_b64 = base64.standard_b64encode(image_bytes).decode("ascii")
                data_url = f"data:{block.media_type};base64,{data_b64}"
                out_parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": data_url},
                    }
                )
        base: dict[str, Any] = {"role": msg.role, "content": out_parts}
        # role=tool / assistant.tool_calls paths never carry list-form
        # content (the runtime authors those as plain str), but plumb
        # the metadata through defensively so behaviour is consistent
        # if a future path lifts them.
        if msg.role == "tool":
            base["tool_call_id"] = msg.metadata.get("tool_call_id", "")
        if msg.role == "assistant" and msg.tool_calls:
            base["tool_calls"] = [
                {
                    "id": tc.call_id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": json.dumps(tc.args)},
                }
                for tc in msg.tool_calls
            ]
            # Normalise empty/blank list content to None per OpenAI spec —
            # same DeepSeek strict-vs-OpenAI lenient gap as the str path
            # below. See comment in the str path for full rationale.
            if not out_parts or all(
                p.get("type") == "text" and not str(p.get("text", "")).strip() for p in out_parts
            ):
                base["content"] = None
        return base

    # str path — unchanged from Phase 1.
    base = {"role": msg.role, "content": msg.content}
    if msg.role == "tool":
        base["tool_call_id"] = msg.metadata.get("tool_call_id", "")
    if msg.role == "assistant" and msg.tool_calls:
        # OpenAI/DeepSeek require the assistant's tool_calls to precede the
        # matching role="tool" results (spec 11 soak finding). arguments is a
        # JSON string per the OpenAI function-calling schema.
        base["tool_calls"] = [
            {
                "id": tc.call_id,
                "type": "function",
                "function": {"name": tc.name, "arguments": json.dumps(tc.args)},
            }
            for tc in msg.tool_calls
        ]
        # OpenAI spec: tool-call-only assistant messages MUST have content=None
        # (or omitted), NOT empty string. OpenAI itself is lenient about
        # content="", but DeepSeek strict-rejects ("Messages with role 'tool'
        # must be a response to a preceding message with 'tool_calls'" — the
        # API silently drops a malformed assistant message then orphans the
        # next tool message). NVIDIA Nemotron + tool-call-only emissions
        # (common; the model often emits zero narration before invoking) hit
        # this every turn under Spec 20's MultiModelChatBackend cross-provider
        # fallback. Normalise content="" -> None for OpenAI-spec strictness.
        if not msg.content.strip():
            base["content"] = None
    return base


def _strip_reasoning_for_provider(
    messages: list[dict[str, Any]], provider: str
) -> list[dict[str, Any]]:
    """D-20-X-deepseek-reasoning-strip-invariant.

    DeepSeek returns HTTP 400 if ``reasoning_content`` is echoed in input
    messages. Strip the field from any assistant message dict before sending.
    All other providers are passed through unchanged. The runtime currently
    authors assistant messages as plain str (no reasoning field), so this
    is defensive — guarding future serialisers that lift reasoning into the
    on-wire message payload.

    Args:
        messages: Wire-shape message dicts (already serialised).
        provider: Active backend provider name.

    Returns:
        A new list with ``reasoning_content`` removed from every assistant
        message when ``provider == "deepseek"``; the input list otherwise.
    """
    if provider != "deepseek":
        return messages
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.get("role") == "assistant" and "reasoning_content" in m:
            cleaned = {k: v for k, v in m.items() if k != "reasoning_content"}
            out.append(cleaned)
        else:
            out.append(m)
    return out


def _prepend_shim_to_openai(
    messages: list[dict[str, Any]], tools: list[ToolSpec]
) -> list[dict[str, Any]]:
    """Inject shim instructions as a system message at the front."""
    block = render_tool_instructions(tools)
    if not block:
        return messages
    if messages and messages[0]["role"] == "system":
        messages = [
            {"role": "system", "content": f"{messages[0]['content']}\n\n{block}"},
            *messages[1:],
        ]
    else:
        messages = [{"role": "system", "content": block}, *messages]
    return messages


def _tool_spec_to_anthropic(tool: ToolSpec) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.parameters,
    }


def _openai_tools_covering_history(
    tools: list[ToolSpec], messages: list[ConversationMessage]
) -> list[dict[str, Any]]:
    """OpenAI tool dicts, augmented to cover every tool_call name in the history.

    Strict OpenAI-compatible providers (groq) reject a request if ANY tool_call
    in the message history references a tool absent from ``tools`` — including a
    HALLUCINATED call the model already received an ``is_error`` result for (e.g.
    a weak model calling a *skill* name like ``document_generation`` directly
    instead of via ``use_skill``). Lenient providers (OpenAI proper, Anthropic)
    ignore the history-vs-tools relation; groq validates it and 400s, hard-failing
    the whole turn. So we advertise a synthetic no-op spec for any historical
    tool_call name not already offered — the request validates, and the model does
    not re-call it because it already holds the "not available" result (R4-C1-13).
    """
    out = [_tool_spec_to_openai(t) for t in tools]
    advertised = {d["function"]["name"] for d in out}
    seen: set[str] = set()
    for m in messages:
        for tc in m.tool_calls or ():
            if tc.name in advertised or tc.name in seen:
                continue
            seen.add(tc.name)
            out.append(
                {
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "description": "(unavailable — do not call this; it is not a tool)",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            )
    return out


def _tool_spec_to_openai(tool: ToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        },
    }


# ----------------------------------------------------------------------
# Response parsers
# ----------------------------------------------------------------------


def _parse_anthropic_response(
    response: Any,  # noqa: ANN401 — SDK type
    provider: str,
    use_native_tools: bool,
) -> ChatResponse:
    content_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    content_blocks = getattr(response, "content", []) or []
    for block in content_blocks:
        block_type = getattr(block, "type", "")
        if block_type == "text":
            content_parts.append(getattr(block, "text", ""))
        elif block_type == "tool_use":
            tool_calls.append(
                ToolCall(
                    name=getattr(block, "name", ""),
                    args=dict(getattr(block, "input", {}) or {}),
                    call_id=getattr(block, "id", ""),
                )
            )
    content = "".join(content_parts)
    if not use_native_tools:
        content, parsed = parse_tool_calls(content)
        tool_calls.extend(parsed)

    usage_obj = getattr(response, "usage", None)
    prompt_tokens = getattr(usage_obj, "input_tokens", 0) if usage_obj else 0
    completion_tokens = getattr(usage_obj, "output_tokens", 0) if usage_obj else 0
    usage = TokenUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
    )
    return ChatResponse(
        content=content,
        tool_calls=tool_calls,
        usage=usage,
        model=getattr(response, "model", "") or "",
        provider=provider,
        latency_ms=0.0,  # set by caller
    )


def _recover_textual_tool_call(content: str) -> ToolCall | None:
    """Recover a tool call a native-tools model serialised into ``content`` text.

    Some OpenRouter-fronted models, when handed native ``tools``, emit the tool
    call as a JSON object in the assistant *content* instead of populating the
    structured ``tool_calls`` field — the OpenAI serialisation shape
    ``{"type":"function","name":...,"parameters":{...}}`` (or the bare
    ``{"name":...,"arguments":{...}}`` shape). Left unrecovered, the agentic loop
    treats that text as a reasoning step and surfaces the raw JSON to the user.

    The recovery is deliberately strict to avoid false positives: the *entire*
    trimmed content (optionally inside a ```` ```json ```` fence) must parse as a
    single JSON object that carries a string ``name`` AND an ``arguments`` /
    ``parameters`` object (or ``"type": "function"``). Prose that merely embeds a
    JSON snippet does not match (it is not standalone JSON), so normal replies are
    never mistaken for tool calls.

    Args:
        content: The assistant message content returned by the provider.

    Returns:
        A :class:`ToolCall` when the content is unambiguously a serialised tool
        call, else ``None`` (the caller keeps the content as text).
    """
    text = content.strip()
    if text.startswith("```"):
        # Strip a single fenced block (```json ... ``` or ``` ... ```).
        inner = text[3:]
        if inner[:4].lower().startswith("json"):
            inner = inner[4:]
        inner = inner.strip()
        if inner.endswith("```"):
            inner = inner[:-3].strip()
        text = inner
    if not (text.startswith("{") and text.endswith("}")):
        return None
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    # ``{"type":"function","function":{"name":...,"arguments":...}}`` (nested) or
    # the flattened ``{"type":"function","name":...,"parameters":...}`` variant.
    inner_fn = obj.get("function")
    candidate = inner_fn if isinstance(inner_fn, dict) else obj
    name = candidate.get("name")
    if not isinstance(name, str) or not name:
        return None
    raw_args = candidate.get("arguments")
    if raw_args is None:
        raw_args = candidate.get("parameters")
    is_function = obj.get("type") == "function"
    if raw_args is None and not is_function:
        # No args field AND no function marker → not confidently a tool call.
        return None
    if isinstance(raw_args, str):
        try:
            raw_args = json.loads(raw_args)
        except (json.JSONDecodeError, ValueError):
            raw_args = {}
    args = raw_args if isinstance(raw_args, dict) else {}
    return ToolCall(name=name, args=args, call_id="")


def _parse_openai_response(
    response: Any,  # noqa: ANN401 — SDK type
    provider: str,
    use_native_tools: bool,
) -> ChatResponse:
    choices = getattr(response, "choices", []) or []
    content = ""
    tool_calls: list[ToolCall] = []
    if choices:
        message = getattr(choices[0], "message", None)
        content = (getattr(message, "content", None) or "") if message else ""
        # ``finish_reason="length"`` means the provider cut the response off at
        # the token budget — a tool call's ``arguments`` JSON is then likely
        # truncated mid-object. Used as corroborating evidence below.
        finish_reason = getattr(choices[0], "finish_reason", None)
        for tc in getattr(message, "tool_calls", []) or []:
            fn = getattr(tc, "function", None)
            if fn is None:
                continue
            raw_args = getattr(fn, "arguments", "{}") or "{}"
            truncated = False
            try:
                args_dict = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except json.JSONDecodeError:
                # The model wrote a payload (e.g. a large ``code`` string) that
                # exceeded the response budget and was cut off mid-JSON. Do NOT
                # silently coerce to empty args and dispatch — that produces the
                # cryptic "Field required" and an identical-retry loop. Mark the
                # call truncated so the runtime returns actionable guidance
                # (TRUNCATED_TOOL_CALL_MESSAGE) instead of dispatching empty args.
                args_dict = {}
                truncated = True
            if not isinstance(args_dict, dict):
                args_dict = {}
                truncated = True
            tool_calls.append(
                ToolCall(
                    name=getattr(fn, "name", ""),
                    args=args_dict,
                    call_id=getattr(tc, "id", "") or "",
                    truncated=truncated or finish_reason == "length",
                )
            )
    if not use_native_tools:
        content, parsed = parse_tool_calls(content)
        tool_calls.extend(parsed)
    elif not tool_calls and content:
        # Native-tools path: the SDK returned no structured tool_calls. Some
        # OpenRouter-fronted models serialise the tool call into the content text
        # instead (the OpenAI ``{"type":"function",...}`` shape). Recover it so the
        # agentic loop dispatches a real call rather than surfacing raw JSON to the
        # user as a reasoning step.
        recovered = _recover_textual_tool_call(content)
        if recovered is not None:
            tool_calls.append(recovered)
            content = ""

    usage_obj = getattr(response, "usage", None)
    prompt_tokens = getattr(usage_obj, "prompt_tokens", 0) if usage_obj else 0
    completion_tokens = getattr(usage_obj, "completion_tokens", 0) if usage_obj else 0
    usage = TokenUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
    )
    return ChatResponse(
        content=content,
        tool_calls=tool_calls,
        usage=usage,
        model=getattr(response, "model", "") or "",
        provider=provider,
        latency_ms=0.0,
    )
