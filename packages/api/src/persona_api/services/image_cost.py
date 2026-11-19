"""What one image generation call cost us, for every charge site that bills one (R9-198).

There are two ways to know, and they are priced on different bases:

* **The whole call.** ``compute_turn_cost`` prices the result's own reported cost (the
  OpenRouter ``usage.cost`` actual) or, failing that, its token usage. Either figure already
  covers every image the call produced, so it is used as is.
* **One image.** A backend that reports neither (openai, fal, cloudflare, nvidia) is priced
  from its registry row, and a row's price is for ONE image. That price is multiplied by the
  number of images the provider actually RETURNED, which is what it billed us for. The count
  requested is deliberately not used: a provider may send back fewer than were asked for,
  and a person should not pay for an image that never arrived.

The multiplication lives here, at the charge site, and not in the price table: the table
states a unit price, and how many units a call produced is a fact about the call. Before
this, the per-image price went straight into the charge, so a two-image request paid for
one. The request path's true-up and the avatar charge both price through this one function
so the rule cannot drift between them again.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.billing.image_pricing import image_cents
from persona_runtime.cost import CENTS_ROUND_DECIMALS, compute_turn_cost

if TYPE_CHECKING:
    from persona.billing.basis import CostBasis
    from persona.imagegen import GenerationResult
    from persona_runtime.cost import CostSource

__all__ = ["generation_cost_cents"]


def generation_cost_cents(
    result: GenerationResult, *, cost_source: CostSource | None
) -> tuple[float, CostBasis]:
    """The provider cost in cents of the whole generation call, and its basis.

    A cost the call reported or that its token usage prices is returned unchanged. Only
    the per-image registry fallback is scaled, by ``len(result.images)``. ``unpriced``
    comes back as ``(0.0, "unpriced")`` and the caller keeps its floor.
    """
    cost_cents, basis = compute_turn_cost(
        provider=result.provider,
        model=result.model,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        actual_cost_usd=result.cost_usd,
        source=cost_source,
    )
    if basis != "unpriced":
        return cost_cents, basis
    per_image_cents, basis = image_cents(result.provider, model=result.model)
    # Rounded the way every other cents figure is, so a count above two cannot leave a
    # float noise bit that the credit ceil would turn into an extra credit.
    return round(per_image_cents * len(result.images), CENTS_ROUND_DECIMALS), basis
