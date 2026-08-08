"""Model backends — the abstraction between Persona and every LLM.

Public surface: the :class:`ChatBackend` Protocol, the response types, the
:class:`BackendConfig` settings, the five domain exceptions, and the
:func:`load_backend` factory. Concrete classes are importable for advanced
callers but the recommended entry point is :func:`load_backend`.

See ``docs/specs/spec_02/spec_02_backends.md`` for the spec and
``docs/specs/spec_02/decisions.md`` for D-02-1..D-02-18.
"""

from __future__ import annotations

from persona.backends._factory import load_backend
from persona.backends.config import BackendConfig, Provider
from persona.backends.credentials import (
    ProviderCredentialResolver,
    ProviderCredentials,
    TierResolution,
    parse_models_list,
    resolve_tier_config,
)
from persona.backends.errors import (
    AllModelsFailedError,
    AuthenticationError,
    BackendTimeoutError,
    DegenerateCompletionError,
    IncompleteTierConfigError,
    LocalProviderInModelsListError,
    MalformedTierModelsError,
    ModelNotFoundError,
    NoVisionCapableModelError,
    ProviderCredentialMissingError,
    ProviderError,
    RateLimitError,
    TierNotConfiguredError,
)
from persona.backends.multi_model import AttemptRecord, MultiModelChatBackend
from persona.backends.ollama import OllamaBackend
from persona.backends.openai_compat import OpenAICompatibleBackend, vision_supported
from persona.backends.protocol import ChatBackend
from persona.backends.types import (
    ChatResponse,
    StreamChunk,
    TokenUsage,
    ToolCallDelta,
    ToolSpec,
    tool_spec_from_tool,
)

__all__ = [
    "AllModelsFailedError",
    "AttemptRecord",
    "AuthenticationError",
    "BackendConfig",
    "BackendTimeoutError",
    "DegenerateCompletionError",
    "ChatBackend",
    "ChatResponse",
    "IncompleteTierConfigError",
    "LocalProviderInModelsListError",
    "MalformedTierModelsError",
    "ModelNotFoundError",
    "MultiModelChatBackend",
    "NoVisionCapableModelError",
    "OllamaBackend",
    "OpenAICompatibleBackend",
    "Provider",
    "ProviderCredentialMissingError",
    "ProviderCredentialResolver",
    "ProviderCredentials",
    "ProviderError",
    "RateLimitError",
    "StreamChunk",
    "TierNotConfiguredError",
    "TierResolution",
    "TokenUsage",
    "ToolCallDelta",
    "ToolSpec",
    "load_backend",
    "parse_models_list",
    "resolve_tier_config",
    "tool_spec_from_tool",
    "vision_supported",
]
