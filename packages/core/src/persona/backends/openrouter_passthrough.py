"""One OpenRouter backend serves ANY catalog id (Spec M1 §3 passthrough).

OpenRouter is OpenAI-compatible and takes the model id per request, so the
persona's ``preferred_model`` does not need to be pre-registered in a tier
list: this module builds (and process-caches) a ChatBackend for an arbitrary
id via the SAME factory path ``PERSONA_<TIER>_MODELS`` entries use for the
``openrouter/`` provider. Fail-open: no key configured / construction error ⇒
``None`` — the caller (the loop's preferred-model hook) falls to the tier
default, so a misconfigured choice can never break a turn.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING

from persona.logging import get_logger

if TYPE_CHECKING:
    from persona.backends.protocol import ChatBackend

__all__ = ["OPENROUTER_KEY_ENV", "build_openrouter_passthrough"]

_log = get_logger("backends.openrouter_passthrough")

# The exact env var ``persona.backends.credentials.ProviderCredentialResolver``
# reads for provider "openrouter" (``key_var = f"PERSONA_{provider.upper()}_API_KEY"``
# — verified against that source, plus ``.env.example``, ``persona_runtime
# .openrouter_subscription._API_KEY_ENV``, and ``persona_api.services
# .runtime_factory``, which all read this same literal name).
OPENROUTER_KEY_ENV = "PERSONA_OPENROUTER_API_KEY"


@lru_cache(maxsize=64)
def build_openrouter_passthrough(model_id: str) -> ChatBackend | None:
    """A cached ChatBackend for ``model_id`` via OpenRouter, or ``None`` (fail-open)."""
    import os

    if not os.environ.get(OPENROUTER_KEY_ENV, "").strip():
        _log.debug("openrouter passthrough unavailable: key env unset")
        return None
    if not model_id.strip():
        return None
    try:
        # The real per-provider builder ``PERSONA_<TIER>_MODELS`` "openrouter/<id>"
        # entries go through (persona_runtime.tier._tier_config_from_models_list):
        # resolve credentials for the provider, build a BackendConfig from them,
        # dispatch through the factory. No single-call convenience wrapper exists
        # for this in persona-core, so it is reproduced verbatim here from the
        # same three core primitives that call site uses (core cannot import
        # persona_runtime — the dependency only runs the other way).
        from persona.backends._factory import load_backend
        from persona.backends.config import BackendConfig
        from persona.backends.credentials import ProviderCredentialResolver

        creds = ProviderCredentialResolver().resolve("openrouter")
        config = BackendConfig(
            provider="openrouter",
            model=model_id,
            api_key=creds.api_key,
            base_url=creds.base_url or None,
        )
        return load_backend(config)
    except Exception:  # noqa: BLE001 — a bad choice must never break a turn
        _log.opt(exception=True).warning(
            "openrouter passthrough construction failed (model={m}); falling to tier",
            m=model_id,
        )
        return None
