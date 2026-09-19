"""An image is priced from the registry when the provider does not report its own cost.

Only the OpenRouter image backend puts ``cost_usd`` on its result; openai, fal, nvidia and
cloudflare leave the ``GenerationResult`` defaults (0 tokens, no cost). ``compute_turn_cost``
prices per-token, so zero tokens and no actual priced every one of those images at 0.0 cents,
basis ``unpriced``, and the true-up then charged the 1-credit floor. A roughly 4 cent image
billed 1 cent, and the gap was silent: the only signal was a generic "no pricing metadata"
warning nobody reads.

This resolver is the image half of what ``voice_pricing`` already does for STT and TTS: look up
the served ``(surface, provider)`` row in the one pricing registry M2 established, and price the
unit from it. An unknown provider still returns ``unpriced``, because a made-up provider price
on a real billing path would be a worse defect than the one being fixed: it overcharges a
person rather than undercharging the house.
"""

from __future__ import annotations

from persona.billing.image_pricing import image_cents


def test_a_registry_priced_provider_is_priced_from_its_row() -> None:
    """The OpenRouter image row is the one image price the repo documents (4.0 cents)."""
    cents, basis = image_cents("openrouter", model="gpt-image-2")

    assert cents == 4.0
    assert basis == "estimate_catalog"


def test_an_unknown_provider_stays_unpriced() -> None:
    """No row means we genuinely do not know: say so rather than invent a number."""
    cents, basis = image_cents("fake", model="fake-1")

    assert cents == 0.0
    assert basis == "unpriced"


def test_an_unknown_model_on_a_known_provider_still_prices() -> None:
    """A provider we price, on a SKU we have not listed, is priced at its cheapest known row.

    Mirrors ``voice_pricing._served_row``: ambiguity resolves downwards, so an unlisted SKU can
    never over-charge on a guess.
    """
    cents, basis = image_cents("openrouter", model="some-unlisted-slug")

    assert cents == 4.0
    assert basis == "estimate_catalog"


def test_the_price_is_per_image_not_per_token() -> None:
    """The unit is the image itself: a backend reporting no tokens still owes its real cost."""
    assert image_cents("openrouter", model="gpt-image-2")[0] == image_cents("openrouter")[0]
