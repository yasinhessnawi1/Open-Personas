"""Unit tests for voice resolution — the cloning seam (T07, D-V3-X-cloning-seam-shape).

Verifies the resolution indirection structurally: a persona's ``VoiceSpec``
resolves to a :class:`ResolvedVoice`; the default fallback (D-V3-4); the
fallible error paths (no default, provider mismatch, catalogue miss); and
the always-``True`` ``ai_generated`` provenance flag. Cloning is NOT
implemented — only the seam is exercised.
"""

from __future__ import annotations

import pytest
from persona.schema.persona import CatalogueVoice
from persona_voice.tts import ResolvedVoice, TTSVoiceNotFoundError
from persona_voice.tts.voice_resolution import resolve_voice


def _spec(provider: str = "cartesia", voice_id: str = "voice-1") -> CatalogueVoice:
    return CatalogueVoice(provider=provider, voice_id=voice_id)


# ---------- happy path -----------------------------------------------------


def test_resolves_spec_to_resolved_voice() -> None:
    rv = resolve_voice(_spec(), provider="cartesia")
    assert isinstance(rv, ResolvedVoice)
    assert rv.provider == "cartesia"
    assert rv.voice_ref == "voice-1"
    assert rv.ai_generated is True
    assert rv.addressing == {}


def test_falls_back_to_default_when_spec_is_none() -> None:
    rv = resolve_voice(None, provider="cartesia", default_voice_id="default-v")
    assert rv.voice_ref == "default-v"


def test_catalogue_membership_passes_when_voice_present() -> None:
    rv = resolve_voice(
        _spec(voice_id="v2"),
        provider="cartesia",
        allowed_voice_ids={"v1", "v2", "v3"},
    )
    assert rv.voice_ref == "v2"


# ---------- fallible error paths -------------------------------------------


def test_no_spec_and_no_default_raises() -> None:
    with pytest.raises(TTSVoiceNotFoundError) as exc:
        resolve_voice(None, provider="cartesia")
    assert exc.value.context["provider"] == "cartesia"


def test_provider_mismatch_without_default_raises() -> None:
    # D-V14-13: mismatch with NO default to fall back to still fails fast.
    with pytest.raises(TTSVoiceNotFoundError) as exc:
        resolve_voice(_spec(provider="elevenlabs"), provider="cartesia")
    assert exc.value.context["voice"] == "voice-1"


def test_provider_mismatch_falls_back_to_default() -> None:
    """Spec V14 (D-V14-13): a persona voice addressed to a DIFFERENT provider
    than the active backend FAILS SOFT to the active provider's default (rather
    than crashing the call) — the backstop for the auto-remap window."""
    rv = resolve_voice(
        _spec(provider="cartesia", voice_id="cartesia-voice"),
        provider="elevenlabs",
        default_voice_id="eleven-default",
    )
    assert rv.provider == "elevenlabs"
    assert rv.voice_ref == "eleven-default"  # the active provider's default, NOT the stored id


def test_voice_not_in_catalogue_raises() -> None:
    with pytest.raises(TTSVoiceNotFoundError) as exc:
        resolve_voice(
            _spec(voice_id="missing"),
            provider="cartesia",
            allowed_voice_ids={"v1", "v2"},
        )
    assert exc.value.context["voice"] == "missing"


def test_default_also_checked_against_catalogue() -> None:
    with pytest.raises(TTSVoiceNotFoundError):
        resolve_voice(
            None,
            provider="cartesia",
            default_voice_id="not-in-set",
            allowed_voice_ids={"v1"},
        )
