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


# --- the backends that report nothing are now priced from their own vendor's list price ---
#
# Each number here is the vendor's published price for the request this product actually
# makes (1024x1024 at the default quality); the row's comment in the registry carries the
# page and the date it was read. These tests are what stops a silent edit of those numbers.


def test_an_openai_image_is_priced_at_the_published_medium_quality_price() -> None:
    """gpt-image-1 at 1024x1024 medium is $0.042, and "standard" maps to medium at the wire."""
    cents, basis = image_cents("openai", model="gpt-image-1")

    assert cents == 4.2
    assert basis == "estimate_static"


def test_a_fal_image_is_priced_at_one_megapixel() -> None:
    """FLUX 1.1 [pro] is $0.04 per megapixel and a square 1024 image is one megapixel."""
    cents, basis = image_cents("fal", model="fal-ai/flux-pro/v1.1")

    assert cents == 4.0
    assert basis == "estimate_static"


def test_a_cloudflare_image_is_priced_at_its_tiles_plus_its_steps() -> None:
    """flux-1-schnell: 4 tiles at $0.0000528 plus the 4 steps this backend sends at $0.0001056.

    Sub-cent, so the credit floor swallows it at charge time. The row still matters: the
    ledger records the REAL cost, and "0.06336 cents" and "we have no idea" are not the same
    fact about the money.
    """
    cents, basis = image_cents("cloudflare", model="@cf/black-forest-labs/flux-1-schnell")

    assert cents == 0.06336
    assert basis == "estimate_static"


def test_nvidia_is_still_honestly_unpriced() -> None:
    """NVIDIA publishes no per-image price for the hosted catalog, so there is no row to add.

    This is the deliberate gap, asserted so that adding a guessed nvidia price has to go
    through a test that says out loud what it is doing.
    """
    cents, basis = image_cents("nvidia", model="nvidia/flux.2-klein-4b")

    assert cents == 0.0
    assert basis == "unpriced"


def test_an_unlisted_cloudflare_model_resolves_downwards_never_upwards() -> None:
    """The SD-family models Cloudflare no longer prices resolve to the cheapest row we hold."""
    cents, _ = image_cents("cloudflare", model="@cf/stabilityai/stable-diffusion-xl-base-1.0")

    assert cents <= 0.06336
