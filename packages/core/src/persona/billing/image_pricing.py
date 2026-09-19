"""What one generated image costs us (Spec M3; the image half of ``voice_pricing``).

Only the OpenRouter image backend reports its own cost: it sets ``cost_usd`` on the
``GenerationResult`` and the true-up prices that actual. openai, fal, nvidia and cloudflare
leave the result's defaults (zero tokens, no cost), and ``compute_turn_cost`` prices per TOKEN,
so every one of their images priced at 0.0 cents with basis ``unpriced`` and the true-up
charged the 1-credit floor: 50 credits pre-deducted, 49 refunded, a roughly 4 cent image
recovered as 1 cent. Nothing failed, which is why it lasted.

This resolves the image's cost from the same registry ``voice_pricing`` reads for STT and TTS,
so a backend that reports nothing is still billed what its row says it costs.

**Where there is no row, this returns ``unpriced`` and the caller keeps the floor.** That is
deliberate. A provider price nobody has verified would be a worse defect than the one this
closes: undercharging costs the house money, while a guessed price overcharges a person. The
four backends above need real per-image prices from the owner's own invoices before their rows
can exist; until then the gap is loud (the caller names the provider in a warning) rather than
silent, which is the part that actually went wrong here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.billing.pricing_registry import served_row

if TYPE_CHECKING:
    from persona.billing.basis import CostBasis

__all__ = ["image_cents"]

_SURFACE = "image"


def image_cents(provider: str, *, model: str | None = None) -> tuple[float, CostBasis]:
    """The provider's real cost for ONE generated image, from its registry row.

    The unit is the image, not the token, so a backend that reports no usage is priced the
    same as one that does. Unknown provider gives ``(0.0, "unpriced")``, see the module
    docstring for why that is a refusal rather than an oversight.
    """
    row = served_row(_SURFACE, provider, model)
    if row is None or row.provider_cost_cents is None:
        return 0.0, "unpriced"
    return row.provider_cost_cents, row.cost_basis
