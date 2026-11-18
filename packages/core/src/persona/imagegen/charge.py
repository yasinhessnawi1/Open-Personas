"""The charge seam of the ``generate_image`` tool (R9-308).

The tool lives in core, and core does not bill: the credits ledger, the edition's policy
and the owner of the request all belong to the api. Until R9-308 nothing bridged the two,
so an image a persona made in a chat turn was charged nothing at all. The tool now takes
an injected :class:`ImageChargeHook` and drives it in a fixed order:

1. :meth:`ImageChargeHook.authorize` runs BEFORE the provider is called. It may refuse by
   raising :class:`ImageChargeRefusedError`, in which case the provider is never called
   and the model gets a structured tool error it can explain to the person. It may also
   return ``None`` to say there is nothing to bill (no request owner, the CLI).
2. :meth:`ImageCharge.settle` runs only AFTER the provider returned images, with that
   result, so the charge follows what was actually made. A provider failure settles
   nothing. A settle failure is logged by the tool and never takes away an image the
   person already has (the provider has billed us by then either way).

Core defines only these shapes; the api implements them over its credits policy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from persona.imagegen.errors import ImageGenError

if TYPE_CHECKING:
    from persona.imagegen.result import GenerationResult, ImageGenOptions

__all__ = ["ImageCharge", "ImageChargeHook", "ImageChargeRefusedError"]


class ImageChargeRefusedError(ImageGenError):
    """The person cannot pay for this image, so the provider was not called.

    The message is shown to the model as the tool error, so it should say plainly what
    happened and what the person can do about it.
    """


@runtime_checkable
class ImageCharge(Protocol):
    """One authorized generation's pending charge."""

    async def settle(self, result: GenerationResult) -> None:
        """Charge for ``result``, the images the provider actually returned."""
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
