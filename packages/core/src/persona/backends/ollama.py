"""Ollama chat backend.

Talks to a local Ollama instance over its native HTTP API at
``/api/chat`` via raw ``httpx`` — we deliberately do not pull the
``ollama`` Python package (it would be a thin wrapper for one
dependency we already have).

Defaults to the prompt-based tool-calling shim (D-02-9). Native tools
can be enabled per-instance via ``OllamaBackend(use_native_tools=True)``
for callers who know their model supports it and accept Ollama's
inconsistent streaming-with-tools shape (issue #12557).
"""

from __future__ import annotations

import base64
import json
import time
from typing import TYPE_CHECKING, Any

import httpx

from persona.backends._tool_shim import (
    ShimState,
    new_shim_state,
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
from persona.logging import get_logger
from persona.schema.content import ImageContent, TextContent
from persona.schema.tools import ToolCall

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from persona.schema.conversation import ConversationMessage


__all__ = ["OllamaBackend"]


_LOG = get_logger("backends.ollama")


class OllamaBackend:
    """Async chat backend for a local (or proxied) Ollama instance.

    Construction is cheap: no health check, no network call (D-02-9).
    Use :meth:`ping` for an explicit reachability test.
    """

    def __init__(
        self,
        config: BackendConfig,
        *,
        use_native_tools: bool = False,
        use_vision: bool = False,
        workspace_root: Path | None = None,
    ) -> None:
        """Construct.

        Args:
            config: Backend configuration; ``provider`` must be ``ollama``.
            use_native_tools: Opt into Ollama's native tool-calling
                surface (D-02-9). Default ``False`` uses the prompt-based
                shim.
            use_vision: Opt into Ollama's native vision surface (the
                ``images`` field on user messages). Default ``False`` is
                fail-loud — any :class:`ImageContent` block in the
                message prefix raises :class:`BackendVisionNotSupportedError`
                at ``chat`` / ``chat_stream`` entry before any HTTP
                round-trip. Mirrors the ``use_native_tools`` opt-in
                shape (D-02-9 / D-13-3 / D-13-X-error-hierarchy).
            workspace_root: Required when ``use_vision=True`` and a
                multimodal turn carries :class:`ImageContent` — used to
                resolve workspace-path refs to bytes (D-13-2). When
                ``None`` and an image is present, the same
                :class:`BackendVisionNotSupportedError` is raised so
                the missing-workspace failure mode is loud rather than
                a silent text-only drop.
        """
        if config.provider != "ollama":
            raise ProviderError(
                f"OllamaBackend got provider={config.provider!r}",
                context={"provider": config.provider},
            )
        self._config = config
        self._model = config.model
        self._timeout = config.request_timeout_s
        self._base_url = (config.base_url or DEFAULT_BASE_URLS["ollama"]).rstrip("/")
        self._use_native_tools = use_native_tools
        self._use_vision = use_vision
        self._workspace_root = workspace_root
        self._client: httpx.AsyncClient | None = None
        # R9-046: carries the shim's synthetic call_id numbering forward
        # across rounds of one multi-round agentic turn. This instance is
        # cached + reused by the runtime's TierRegistry for the life of the
        # process, so a monotonically-increasing counter here never resets
        # mid-turn (a strict superset of "unique within one turn").
        self._shim_call_seq = 0
        # Ollama behind a proxy may require an Authorization header.
        api_key = config.api_key.get_secret_value() if config.api_key else None
        self._auth_header: dict[str, str] = (
            {"Authorization": f"Bearer {api_key}"} if api_key else {}
        )
        _LOG.debug(
            "constructed",
            provider="ollama",
            model=self._model,
            base_url=self._base_url,
            use_native_tools=self._use_native_tools,
            use_vision=self._use_vision,
        )

    @property
    def provider_name(self) -> str:
        return "ollama"

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def supports_native_tools(self) -> bool:
        return self._use_native_tools

    @property
    def supports_vision(self) -> bool:
        return self._use_vision

    async def aclose(self) -> None:
        """Close the underlying ``httpx`` client. Idempotent."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def ping(self) -> bool:
        """Explicit health check. Returns True if ``/api/tags`` responds 2xx."""
        client = self._ensure_client()
        try:
            response = await client.get("/api/tags")
        except httpx.HTTPError:
            return False
        return response.is_success

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=httpx.Timeout(connect=10.0, read=self._timeout, write=10.0, pool=10.0),
                headers=self._auth_header,
            )
        return self._client

    def _guard_vision(self, messages: list[ConversationMessage]) -> None:
        """Fail loud at backend entry when vision is required but unavailable.

        Scans the message prefix for :class:`ImageContent` blocks. When
        the count is non-zero and ``self.supports_vision is False``,
        raises :class:`BackendVisionNotSupportedError` with the
        D-13-X-error-hierarchy context shape BEFORE any HTTP round-trip
        begins. Called from both :meth:`chat` and :meth:`chat_stream`
        as the first line of work.
        """
        image_count = 0
        for msg in messages:
            if isinstance(msg.content, list):
                image_count += sum(1 for b in msg.content if isinstance(b, ImageContent))
        if image_count and not self._use_vision:
            raise BackendVisionNotSupportedError(
                "ollama backend not opted into vision",
                context={
                    "backend": "ollama",
                    "model": self._model,
                    "image_count": str(image_count),
                },
            )

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
        """Single-shot chat. See ``ChatBackend.chat`` for contract."""
        self._guard_vision(messages)
        body = self._build_body(
            messages, tools, temperature, max_tokens, stop, stream=False, top_p=top_p, top_k=top_k
        )
        started = time.perf_counter()
        try:
            response = await self._ensure_client().post("/api/chat", json=body)
            self._raise_for_status(response)
            payload = response.json()
        except httpx.HTTPError as exc:
            self._reraise_httpx(exc)
        latency_ms = (time.perf_counter() - started) * 1000.0
        return self._parse_chat_response(payload, latency_ms, bool(tools))

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
        """Streaming chat. See ``ChatBackend.chat_stream`` for contract."""
        self._guard_vision(messages)
        body = self._build_body(
            messages, tools, temperature, max_tokens, stop, stream=True, top_p=top_p, top_k=top_k
        )
        # R9-046: resume synthetic call_id numbering from the prior round
        # instead of resetting to shim-1 every round.
        shim_state: ShimState | None = (
            new_shim_state(starting_call_seq=self._shim_call_seq)
            if (tools and not self._use_native_tools)
            else None
        )
        try:
            async with self._ensure_client().stream("POST", "/api/chat", json=body) as response:
                self._raise_for_status(response)
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    obj = json.loads(line)
                    async for chunk in self._chunks_from_line(obj, shim_state):
                        yield chunk
                    if obj.get("done"):
                        break
        except httpx.HTTPError as exc:
            self._reraise_httpx(exc)
        finally:
            if shim_state is not None:
                # Persist this round's final call_seq so the next round
                # (same backend instance) continues numbering forward.
                self._shim_call_seq = shim_state.call_seq

    # ------------------------------------------------------------------
    # Request building
    # ------------------------------------------------------------------

    def _build_body(
        self,
        messages: list[ConversationMessage],
        tools: list[ToolSpec] | None,
        temperature: float,
        max_tokens: int,
        stop: list[str] | None,
        *,
        stream: bool,
        top_p: float | None = None,
        top_k: int | None = None,
    ) -> dict[str, Any]:
        ollama_messages = [self._convert_message(m) for m in messages]
        # In the vision-enabled path the list-form user messages already
        # carry their ``images`` field. The shim-instruction block below
        # only mutates the system message text, so the multimodal user
        # entry passes through untouched.
        use_native = self._use_native_tools and bool(tools)
        if tools and not use_native:
            block = render_tool_instructions(tools)
            if block:
                if ollama_messages and ollama_messages[0]["role"] == "system":
                    ollama_messages[0] = {
                        "role": "system",
                        "content": f"{ollama_messages[0]['content']}\n\n{block}",
                    }
                else:
                    ollama_messages = [
                        {"role": "system", "content": block},
                        *ollama_messages,
                    ]
        body: dict[str, Any] = {
            "model": self._model,
            "messages": ollama_messages,
            "stream": stream,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
        }
        if stop:
            body["options"]["stop"] = stop
        # Ollama's ``options`` block accepts both nucleus + top_k sampling.
        if top_p is not None:
            body["options"]["top_p"] = top_p
        if top_k is not None:
            body["options"]["top_k"] = top_k
        if use_native and tools:
            body["tools"] = [_tool_spec_to_ollama(t) for t in tools]
        return body

    def _convert_message(self, msg: ConversationMessage) -> dict[str, Any]:
        """Translate one ``ConversationMessage`` to Ollama's message shape.

        For ``str`` content (the default path) this is a pass-through.
        For list-form content (Spec 13 T03 widening), the text blocks
        are concatenated into ``content`` and any :class:`ImageContent`
        blocks are resolved from ``workspace_root`` and base64-encoded
        into the ``images`` field on the user message (Ollama's native
        vision wire shape). When the message carries an image and
        ``workspace_root`` is unset, raise
        :class:`BackendVisionNotSupportedError` so the missing-workspace
        failure mode stays loud (mirrors the openai_compat T05/T06
        guard).
        """
        if isinstance(msg.content, list):
            image_count = sum(1 for b in msg.content if isinstance(b, ImageContent))
            if image_count and self._workspace_root is None:
                raise BackendVisionNotSupportedError(
                    "no workspace_root configured for image resolution",
                    context={
                        "backend": "ollama",
                        "model": self._model,
                        "image_count": str(image_count),
                        "reason": "missing_workspace_root",
                    },
                )
            text_parts: list[str] = []
            images_b64: list[str] = []
            for block in msg.content:
                if isinstance(block, TextContent):
                    text_parts.append(block.text)
                elif isinstance(block, ImageContent):
                    assert self._workspace_root is not None  # narrowed above
                    image_bytes = (self._workspace_root / block.workspace_path).read_bytes()
                    images_b64.append(base64.standard_b64encode(image_bytes).decode("ascii"))
            out: dict[str, Any] = {
                "role": msg.role,
                "content": "\n\n".join(text_parts),
            }
            if images_b64:
                out["images"] = images_b64
            if msg.role == "tool":
                out["tool_call_id"] = msg.metadata.get("call_id", "")
            return out
        out = {"role": msg.role, "content": msg.content}
        if msg.role == "tool":
            out["tool_call_id"] = msg.metadata.get("call_id", "")
        return out

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def _parse_chat_response(
        self, payload: dict[str, Any], latency_ms: float, tools_present: bool
    ) -> ChatResponse:
        message = payload.get("message", {}) or {}
        content = message.get("content", "") or ""
        tool_calls: list[ToolCall] = []
        for tc in message.get("tool_calls", []) or []:
            fn = tc.get("function", {}) if isinstance(tc, dict) else {}
            name = fn.get("name", "") if isinstance(fn, dict) else ""
            args = fn.get("arguments", {}) if isinstance(fn, dict) else {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            if not isinstance(args, dict):
                args = {}
            if name:
                tool_calls.append(ToolCall(name=name, args=args))
        if tools_present and not self._use_native_tools:
            content, parsed = parse_tool_calls(content)
            tool_calls.extend(parsed)
        usage = _extract_ollama_usage(payload)
        return ChatResponse(
            content=content,
            tool_calls=tool_calls,
            usage=usage,
            model=payload.get("model", self._model),
            provider="ollama",
            latency_ms=latency_ms,
        )

    async def _chunks_from_line(
        self, obj: dict[str, Any], shim_state: ShimState | None
    ) -> AsyncIterator[StreamChunk]:
        """Translate one Ollama NDJSON line into one or more :class:`StreamChunk`."""
        if obj.get("done"):
            yield StreamChunk(delta="", is_final=True, usage=_extract_ollama_usage(obj))
            return
        message = obj.get("message", {}) or {}
        text = message.get("content", "") or ""
        tool_calls = message.get("tool_calls") or []
        if text:
            if shim_state is not None:
                consumer_text, tc_delta = parse_tool_call_delta(text, shim_state)
                if consumer_text or tc_delta is not None:
                    yield StreamChunk(delta=consumer_text, tool_call_delta=tc_delta)
            else:
                yield StreamChunk(delta=text)
        for tc in tool_calls:
            fn = tc.get("function", {}) if isinstance(tc, dict) else {}
            name = fn.get("name", "") if isinstance(fn, dict) else ""
            args = fn.get("arguments", {}) if isinstance(fn, dict) else {}
            if isinstance(args, dict):
                args_str = json.dumps(args)
            elif isinstance(args, str):
                args_str = args
            else:
                args_str = "{}"
            yield StreamChunk(
                delta="",
                tool_call_delta=ToolCallDelta(
                    call_id="",
                    name_delta=name,
                    arguments_delta=args_str,
                ),
            )

    # ------------------------------------------------------------------
    # Error mapping
    # ------------------------------------------------------------------

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.is_success:
            return
        status = response.status_code
        ctype = response.headers.get("content-type", "")
        try:
            body = response.json() if ctype.startswith("application/json") else {}
        except (json.JSONDecodeError, ValueError):
            body = {}
        error_text = body.get("error", "") if isinstance(body, dict) else ""

        if status in (401, 403):
            raise AuthenticationError(
                f"Ollama returned {status}: {error_text}",
                context={"provider": "ollama"},
            )
        if status == 429:
            ctx: dict[str, str] = {"provider": "ollama"}
            retry_after = response.headers.get("retry-after")
            if retry_after:
                ctx["retry_after_s"] = str(retry_after)
            raise RateLimitError(f"Ollama 429: {error_text}", context=ctx)
        if status == 404 and "model" in error_text.lower():
            raise ModelNotFoundError(
                f"Ollama 404: {error_text}",
                context={"provider": "ollama", "model": self._model},
            )
        raise ProviderError(
            f"Ollama HTTP {status}: {error_text}",
            context={"provider": "ollama", "status": str(status)},
        )

    def _reraise_httpx(self, exc: httpx.HTTPError) -> Any:  # noqa: ANN401 — re-raises
        if isinstance(exc, httpx.TimeoutException):
            raise BackendTimeoutError(str(exc), context={"provider": "ollama"}) from exc
        if isinstance(exc, httpx.ConnectError):
            raise ProviderError(
                str(exc),
                context={"provider": "ollama", "kind": "connection"},
            ) from exc
        raise ProviderError(
            str(exc), context={"provider": "ollama", "underlying": type(exc).__name__}
        ) from exc


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _tool_spec_to_ollama(tool: ToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        },
    }


def _extract_ollama_usage(payload: dict[str, Any]) -> TokenUsage:
    prompt_tokens = int(payload.get("prompt_eval_count", 0) or 0)
    completion_tokens = int(payload.get("eval_count", 0) or 0)
    return TokenUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
    )
