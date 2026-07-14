"""Voice catalogue surface — data-only voice listing (T08, D-V3-3).

The catalogue is exposed so authoring (Spec 10) and management (Spec F5)
can present real voice choices; a persona's ``voice`` selects one of them
and :func:`persona_voice.tts.voice_resolution.resolve_voice` resolves it at
synthesis time. **Data-only at v1** — V3 does NOT render previews
(``preview_url`` is the provider-supplied sample URL passed through for
V6/F5 to use); preview generation / browse UI belongs to V6/F5.

**Provider-agnostic surface, provider-specific fetch.** This module holds
the :class:`VoiceCatalogue` Protocol callers depend on plus the
:func:`normalize_gender` helper; the concrete catalogue fetch lives in the
provider backend (``cartesia_backend.py``) so the vendor SDK stays confined
to that one module (Spec 02 adapter-boundary discipline). The Cartesia
launch backend implements :class:`VoiceCatalogue`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from persona_voice.tts.types import VoiceGender

if TYPE_CHECKING:
    from persona_voice.tts.types import VoiceCatalogueEntry

__all__ = ["VoiceCatalogue", "normalize_gender"]

# Provider gender tags → the normalised :data:`VoiceGender` set. Cartesia
# uses ``masculine``/``feminine``/``gender_neutral``; ElevenLabs (Spec V14)
# uses ``male``/``female`` (+ ``non-binary``) in its ``labels.gender``; anything
# else (incl. ``None``) becomes ``"unspecified"``.
_GENDER_MAP: dict[str, VoiceGender] = {
    "masculine": "masculine",
    "feminine": "feminine",
    "male": "masculine",
    "female": "feminine",
    "gender_neutral": "neutral",
    "neutral": "neutral",
    "non-binary": "neutral",
}


def normalize_gender(raw: str | None) -> VoiceGender:
    """Map a provider gender tag onto the normalised :data:`VoiceGender` set.

    Provider-independent so authoring/management can filter the catalogue
    without knowing each provider's tag vocabulary.
    """
    if raw is None:
        return "unspecified"
    return _GENDER_MAP.get(raw.lower(), "unspecified")


@runtime_checkable
class VoiceCatalogue(Protocol):
    """The data-only voice-catalogue surface (D-V3-3).

    Implemented by the provider backend (the Cartesia launch backend
    conforms). Authoring (Spec 10) and management (F5) depend on this
    Protocol, never on a provider SDK.
    """

    @property
    def provider_name(self) -> str:
        """The provider whose catalogue this lists (matches resolution)."""

    async def list_voices(
        self,
        *,
        gender: VoiceGender | None = None,
        language: str | None = None,
        limit: int | None = None,
    ) -> tuple[VoiceCatalogueEntry, ...]:
        """List available voices with metadata (gender / style / language).

        Args:
            gender: Optional filter on the normalised gender tag.
            language: Optional ISO 639-1 / locale filter.
            limit: Optional cap on the number of voices returned.

        Returns:
            A tuple of :class:`VoiceCatalogueEntry` records (data-only;
            ``preview_url`` is a pass-through sample URL, not rendered here).
        """
