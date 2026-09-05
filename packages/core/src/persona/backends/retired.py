"""Models that providers have retired, and the per-tier filter that drops them (R9-124).

A retired model is not an outage. It fails every call, forever, with a status the chain
did not walk past (NVIDIA's ``410 Gone``), and nothing in the process says so: the
deployed mid chain carried ``nvidia/meta/llama-3.3-70b-instruct`` for ten days after its
end of life, and every voice turn that reached it died there in silence. The classifier
fix in :mod:`persona.backends.multi_model` walks such errors now; this module removes the
dead slot at startup so the chain never pays the round trip at all.

The list is hand-kept and dated, like the Groq record in
``tests/unit/backends/test_groq_catalogue_currency.py``. Adding an entry is the right
move when a provider announces a retirement; deleting one is not, because a future
operator reading an old env var needs to tell a typo from a retirement.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, TypeVar

from persona.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["RETIRED_MODEL_IDS", "filter_retired_models"]

_LOG = get_logger("persona.backends.retired")

#: ``provider/model`` ids providers have withdrawn, with the date it was confirmed.
RETIRED_MODEL_IDS: Final[frozenset[str]] = frozenset(
    {
        # NVIDIA NIM: "has reached its end of life on 2026-08-26T09:00:00Z" (HTTP 410,
        # confirmed 2026-09-05). Sat second in the deployed mid chain of all three apps.
        "nvidia/meta/llama-3.3-70b-instruct",
        # Groq decommissions, confirmed absent from the live catalogue on 2026-09-02 (R9-115).
        "groq/llama-3.3-70b-versatile",
        "groq/meta-llama/llama-4-scout-17b-16e-instruct",
        "groq/llama-3.1-8b-instant",
    }
)

_Slot = TypeVar("_Slot", bound="tuple[str, str]")


def filter_retired_models(models: Sequence[_Slot], *, tier_name: str) -> list[_Slot]:
    """Drop retired ``(provider, model)`` slots from a tier chain, warning per drop.

    Pure. Mirrors ``filter_openrouter_free_mode``: order is preserved, untouched slots
    pass through unchanged, and an empty result is the caller's decision (the tier
    registry skips the tier, the same fail-soft posture as free-mode filtering).

    Args:
        models: Parsed ``(provider, model)`` slots, in chain order.
        tier_name: Tier label for the WARN log.

    Returns:
        The kept slots, preserving order.
    """
    kept: list[_Slot] = []
    for slot in models:
        provider, model = slot
        if f"{provider}/{model}" in RETIRED_MODEL_IDS:
            _LOG.warning(
                "dropping retired model from the chain tier={tier} provider={provider} "
                "model={model}; the provider withdrew it, every call would fail",
                tier=tier_name,
                provider=provider,
                model=model,
            )
            continue
        kept.append(slot)
    return kept
