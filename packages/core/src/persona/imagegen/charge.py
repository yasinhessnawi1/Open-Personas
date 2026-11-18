"""The charge seam of the ``generate_image`` tool (R9-308).

The tool lives in core, and core does not bill: the credits ledger, the edition's policy
and the owner of the request all belong to the api. Until R9-308 nothing bridged the two,
so an image a persona made in a chat turn was charged nothing at all. The tool now takes
an injected :class:`ImageChargeHook` and drives it in a fixed order:

1. :meth:`ImageChargeHook.authorize` runs BEFORE the provider is called. It may refuse by
   raising :class:`ImageChargeRefusedError` (no credits, the daily cap), in which case the
   provider is never called and the model gets a structured tool error it can explain to
   the person. It may also return ``None`` to say there is nothing to bill (no request
   owner, a surface that cannot deliver the image). An authorization may hold money: the
   api takes the estimate up front, so two calls cannot both spend the same balance.
2. :meth:`ImageCharge.settle` runs only AFTER the provider returned images, with that
   result, so the charge follows what was actually made.
3. :meth:`ImageCharge.release` runs instead of settle when the provider call raised, so a
   failed generation charges nothing and anything held is given back.

A ledger failure in settle or release surfaces as :class:`ImageChargeLedgerError`, which
the tool logs and swallows: the image already exists, and failing the tool would take it
away without making the charge any more likely to land. Any other exception is a bug and
is not swallowed. Core defines only these shapes; the api implements them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from persona.imagegen.errors import ImageGenError

if TYPE_CHECKING:
    from persona.imagegen.result import GenerationResult, ImageGenOptions

__all__ = [
    "ImageCharge",
    "ImageChargeHook",
    "ImageChargeLedgerError",
    "ImageChargeRefusedError",
]


class ImageChargeRefusedError(ImageGenError):
    """The person cannot pay for this image, so the provider was not called.

    The message is shown to the model as the tool error, so it should say plainly what
    happened and what the person can do about it. ``context["reason"]`` names why
    (``insufficient_credits``, ``daily_spend_cap``) and becomes the tool result's reason.
    """


class ImageChargeLedgerError(ImageGenError):
    """Settling or releasing a charge could not be written to the ledger.

    Raised by the hook after it has done what it can (retried, recorded the miss); the
    tool logs it and keeps the image.
    """


@runtime_checkable
class ImageCharge(Protocol):
    """One authorized generation's pending charge."""

    async def settle(self, result: GenerationResult) -> None:
        """Charge for ``result``, the images the provider actually returned."""
        ...

    async def release(self) -> None:
        """The provider call failed: charge nothing and give back anything held."""
        ...


@runtime_checkable
class ImageChargeHook(Protocol):
    """Decides, before the provider call, whether an image may be made and who pays."""

    async def authorize(
        self, options: ImageGenOptions, *, provider: str, model: str
    ) -> ImageCharge | None:
        """Return the pending charge, ``None`` when nothing is billed, or refuse.

        Raises:
            ImageChargeRefusedError: The person cannot afford the image.
        """
        ...
