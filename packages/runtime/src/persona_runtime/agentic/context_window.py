"""The context window the agentic loop compacts against (R9-314).

The :class:`~persona_runtime.agentic.compactor.StepHistoryCompactor` fires at 80% of
a token budget, and that budget used to be ``getattr(backend, "max_tokens", 4096)``.
No production backend exposes ``max_tokens``, so every run compacted against 4,096
tokens: past about 3,300 tokens of context, which a tool-heavy run reaches on most
steps. Each compaction is a metered model call (R9-299), so that default cost users
money and threw away context the real model could hold.

The budget now comes from the model metadata the runtime already prices with
(:func:`persona_runtime.cost.served_model_metadata`: static tables first, the
OpenRouter catalog for coverage, never a network fetch on the run path).

The rule, in order:

1. A backend that states its own ``max_tokens`` is believed, exactly as before.
2. Otherwise the budget is the SMALLEST known window among the models that could
   read this context: every member of the step tier's chain (the next step can land
   on any of them), and every member of the background tier that summarises the
   middle of the context when compaction fires (a context that outgrew the
   summariser could not be compacted at all). A model whose window is unknown is
   skipped, and logged once.
3. From that window, :data:`REPLY_RESERVE_TOKENS` is held back for what the
   compactor's count does not see: the reply, the tool schemas, and the gap between
   cl100k and the provider's own tokenizer.
4. When no window is known at all, :data:`FALLBACK_CONTEXT_WINDOW` (the old 4,096)
   is the last resort, logged once per model set.

Cost stays bounded by the separate cost-keyed pruner (Spec W1, D-W1-13), which trims
old tool results long before a wide window fills; this module only decides when the
history no longer fits.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from persona.logging import get_logger

from persona_runtime.cost import served_model_metadata

if TYPE_CHECKING:
    from persona.backends import ChatBackend

    from persona_runtime.cost import CostSource

__all__ = ["FALLBACK_CONTEXT_WINDOW", "REPLY_RESERVE_TOKENS", "compaction_budget"]

_logger = get_logger("agentic.context_window")

#: The last resort when no model's window is known: the budget every run used before.
FALLBACK_CONTEXT_WINDOW: Final[int] = 4096
#: Held back from a known window for the reply, the tool schemas and tokenizer drift.
REPLY_RESERVE_TOKENS: Final[int] = 4096

#: Model sets already logged, so each unknown is reported once per process.
_warned_unknown: set[tuple[tuple[str, str], ...]] = set()
#: Models whose lookup raised, so a broken source is reported once, not every step.
_warned_failed: set[tuple[str, str]] = set()


def _members(backend: ChatBackend | None) -> tuple[tuple[str, str], ...]:
    """The ``(provider, model)`` pairs that may serve a call through ``backend``.

    A multi-model chain lists its members; a bare backend is its own only member.
    """
    if backend is None:
        return ()
    subs = getattr(backend, "backends", None)
    if isinstance(subs, list | tuple) and subs:
        return tuple((sub.provider_name, sub.model_name) for sub in subs)
    return ((backend.provider_name, backend.model_name),)


def _window_of(provider: str, model: str, source: CostSource | None) -> int | None:
    """The model's known context window, or ``None`` when it is not known."""
    try:
        metadata = served_model_metadata(provider, model, source=source)
    except Exception as exc:  # noqa: BLE001 - a broken lookup must not end the run
        if (provider, model) in _warned_failed:
            return None
        _warned_failed.add((provider, model))
        _logger.warning(
            "context window lookup failed provider={provider} model={model} error={error}",
            provider=provider,
            model=model,
            error=type(exc).__name__,
        )
        return None
    return None if metadata is None else metadata.context_length


def _usable(window: int) -> int:
    """The part of a known window the context may fill: the reserve held back.

    Never below half the window, so a small window still leaves room to work in.
    """
    return max(window - REPLY_RESERVE_TOKENS, window // 2)


def _warn_once(unknown: tuple[tuple[str, str], ...], *, fallback: bool) -> None:
    if not unknown or unknown in _warned_unknown:
        return
    _warned_unknown.add(unknown)
    models = ", ".join(f"{provider}/{model}" for provider, model in unknown)
    if fallback:
        _logger.warning(
            "no context window known for {models}; agentic compaction falls back to "
            "{fallback} tokens",
            models=models,
            fallback=FALLBACK_CONTEXT_WINDOW,
        )
    else:
        _logger.info(
            "no context window known for {models}; sizing agentic compaction from the rest",
            models=models,
        )


def compaction_budget(
    step_backend: ChatBackend,
    summariser: ChatBackend | None,
    *,
    source: CostSource | None = None,
) -> int:
    """The token budget the step-history compactor sizes its threshold from.

    Args:
        step_backend: The backend the run's next step goes through.
        summariser: The background-tier backend a compaction is summarised on, or
            ``None`` when it cannot be resolved.
        source: The metadata chain, or ``None`` for the zero-network static tables.

    Returns:
        The budget in tokens; the compactor fires at 80% of it (spec section 6).
    """
    explicit = getattr(step_backend, "max_tokens", None)
    if isinstance(explicit, int) and not isinstance(explicit, bool) and explicit > 0:
        return explicit
    members = tuple(dict.fromkeys((*_members(step_backend), *_members(summariser))))
    windows = {pair: _window_of(*pair, source) for pair in members}
    known = [window for window in windows.values() if window is not None]
    unknown = tuple(pair for pair, window in windows.items() if window is None)
    if not known:
        _warn_once(unknown, fallback=True)
        return FALLBACK_CONTEXT_WINDOW
    _warn_once(unknown, fallback=False)
    return _usable(min(known))
