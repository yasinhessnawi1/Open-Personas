"""The budget the agentic loop compacts against (R9-314).

The :class:`~persona_runtime.agentic.compactor.StepHistoryCompactor` fires at 80% of a
token budget. That budget used to be ``getattr(backend, "max_tokens", 4096)``, and no
production backend exposes ``max_tokens``, so every run compacted against 4,096 tokens.

Sizing it from the model's real window instead is NOT free: a wider budget means every
frontier step carries the whole uncompacted context, and that costs more than the
small-tier compactions it saves (a 19-step run with 4,000 character tool results sends
about 149k frontier input tokens instead of 67k, to save about 25k compaction tokens).
What it buys is fidelity. So the budget is two numbers:

``budget = min(cost budget, window bound)``

(A backend that states its own ``max_tokens`` is the one exception: that number is the
budget, exactly as before. No production backend states one; test fakes and local
backends may.)

- **The cost budget** is the owner's knob (``PERSONA_API_AGENTIC_COMPACTION_BUDGET_TOKENS``
  in the api). Its default, :data:`DEFAULT_COMPACTION_BUDGET_TOKENS`, is today's 4,096, so
  a default deploy pays exactly what it paid before.
- **The window bound** is a hard upper limit, so a raised budget can never exceed what
  every model that may read the context accepts:

  1. Every member of the step tier's chain (the next step can land on any of
     them) and of the background tier (a compaction hands the middle of the context to
     it) is looked up in the metadata the runtime prices with
     (:func:`persona_runtime.cost.served_model_metadata`: static tables first, the
     OpenRouter catalog for coverage, never a network fetch on the run path). The bound
     is the smallest window, minus :data:`REPLY_RESERVE_TOKENS` for the reply, the tool
     schemas and tokenizer drift.
  2. If ANY member's window is unknown, or the background tier cannot be read, the bound
     is the conservative :data:`FALLBACK_CONTEXT_WINDOW`: an unknown fallback may be the
     small one. One WARNING per model set per process, naming whether the catalog is
     still cold (wait) or the model is in no table (add it).

The pruner (Spec W1, D-W1-13) trims old tool results on cost, separately. With its
ceiling at 0 (pruning off) the context is still bounded here, by the window bound.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Literal

from persona.logging import get_logger

from persona_runtime.cost import served_model_metadata

if TYPE_CHECKING:
    from persona.backends import ChatBackend

    from persona_runtime.cost import CostSource

__all__ = [
    "DEFAULT_COMPACTION_BUDGET_TOKENS",
    "FALLBACK_CONTEXT_WINDOW",
    "REPLY_RESERVE_TOKENS",
    "compaction_budget",
    "warn_summariser_unavailable",
]

_logger = get_logger("agentic.context_window")

#: The conservative window bound when any model's window is unknown: the old default.
FALLBACK_CONTEXT_WINDOW: Final[int] = 4096
#: The default cost budget. Today's 4,096, so the default per-run cost is unchanged.
DEFAULT_COMPACTION_BUDGET_TOKENS: Final[int] = 4096
#: Held back from a known window for the reply, the tool schemas and tokenizer drift.
REPLY_RESERVE_TOKENS: Final[int] = 4096

#: Stands in for a background tier that could not be read: an unknown window.
_UNRESOLVED_SUMMARISER: Final[tuple[str, str]] = ("background-tier", "unresolved")

_MissReason = Literal["catalog not warm yet", "not in any metadata table"]

#: What has been logged, so each condition is reported once per process.
_warned_unknown: set[tuple[tuple[tuple[str, str], ...], _MissReason]] = set()
_warned_failed: set[tuple[str, str]] = set()
_warned_summariser: set[str] = set()


def _reset_warnings() -> None:
    """Forget what has been logged (tests)."""
    _warned_unknown.clear()
    _warned_failed.clear()
    _warned_summariser.clear()


def warn_summariser_unavailable(exc: BaseException) -> None:
    """Log, once per process, that the background tier could not be read for the budget."""
    name = type(exc).__name__
    if name in _warned_summariser:
        return
    _warned_summariser.add(name)
    _logger.warning(
        "background tier unavailable for the compaction budget; treating its context "
        "window as unknown error={error}",
        error=name,
    )


def _members(backend: ChatBackend | None) -> tuple[tuple[str, str], ...]:
    """The ``(provider, model)`` pairs that may serve a call through ``backend``.

    A multi-model chain lists its members; a bare backend is its own only member; a
    backend that could not be resolved is one unknown member.
    """
    if backend is None:
        return (_UNRESOLVED_SUMMARISER,)
    subs = getattr(backend, "backends", None)
    if isinstance(subs, list | tuple) and subs:
        return tuple((sub.provider_name, sub.model_name) for sub in subs)
    return ((backend.provider_name, backend.model_name),)


def _window_of(pair: tuple[str, str], source: CostSource | None) -> int | None:
    """The model's known context window, or ``None`` when it is not known."""
    if pair == _UNRESOLVED_SUMMARISER:
        return None
    provider, model = pair
    try:
        metadata = served_model_metadata(provider, model, source=source)
    except Exception as exc:  # noqa: BLE001 - a broken lookup must not end the run
        if pair not in _warned_failed:
            _warned_failed.add(pair)
            _logger.warning(
                "context window lookup failed provider={provider} model={model} error={error}",
                provider=provider,
                model=model,
                error=type(exc).__name__,
            )
        return None
    return None if metadata is None else metadata.context_length


def _miss_reason(source: CostSource | None) -> _MissReason:
    """Why a lookup missed: a cold catalog index, or a model no table carries."""
    return (
        "catalog not warm yet"
        if getattr(source, "catalog_warm", None) is False
        else "not in any metadata table"
    )


def _warn_unknown(unknown: tuple[tuple[str, str], ...], reason: _MissReason) -> None:
    key = (unknown, reason)
    if key in _warned_unknown:
        return
    _warned_unknown.add(key)
    _logger.warning(
        "context window unknown for {models} ({reason}); agentic compaction is bounded "
        "by the {fallback} token fallback",
        models=", ".join(f"{provider}/{model}" for provider, model in unknown),
        reason=reason,
        fallback=FALLBACK_CONTEXT_WINDOW,
    )


def _usable(window: int) -> int:
    """The part of a known window the context may fill: the reserve held back.

    Never below half the window, so a small window still leaves room to work in.
    """
    return max(window - REPLY_RESERVE_TOKENS, window // 2)


def _window_bound(
    step_backend: ChatBackend, summariser: ChatBackend | None, source: CostSource | None
) -> int:
    """The most context every model that may read it accepts (see the module rule)."""
    members = tuple(dict.fromkeys((*_members(step_backend), *_members(summariser))))
    windows = {pair: _window_of(pair, source) for pair in members}
    unknown = tuple(pair for pair, window in windows.items() if window is None)
    if unknown:
        _warn_unknown(unknown, _miss_reason(source))
        return FALLBACK_CONTEXT_WINDOW
    return _usable(min(window for window in windows.values() if window is not None))


def compaction_budget(
    step_backend: ChatBackend,
    summariser: ChatBackend | None,
    *,
    source: CostSource | None = None,
    cost_budget: int = DEFAULT_COMPACTION_BUDGET_TOKENS,
) -> int:
    """The token budget the step-history compactor sizes its threshold from.

    Args:
        step_backend: The backend the run's next step goes through.
        summariser: The background-tier backend a compaction is summarised on, or
            ``None`` when it could not be read (an unknown window).
        source: The metadata chain, or ``None`` for the zero-network static tables.
        cost_budget: The owner's cost knob, in tokens.

    Returns:
        ``min(cost_budget, window bound)``, or the backend's own stated ``max_tokens``
        when it has one (the behaviour before R9-314); the compactor fires at 80% of it.

    Raises:
        ValueError: ``cost_budget`` is not positive.
    """
    if cost_budget <= 0:
        msg = f"the compaction budget must be positive, got {cost_budget}"
        raise ValueError(msg)
    explicit = getattr(step_backend, "max_tokens", None)
    if isinstance(explicit, int) and not isinstance(explicit, bool) and explicit > 0:
        return explicit
    return min(cost_budget, _window_bound(step_backend, summariser, source))
