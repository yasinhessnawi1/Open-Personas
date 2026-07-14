"""The per-call language plan resolved at session build (Spec 32 B2).

``resolve_call_languages`` turns a persona's ``identity.language_default`` into a
single :class:`CallLanguagePlan` carrying the STT route, the TTS route, and the
**reply language** (= what TTS will actually speak, so a TTS fall-back also
steers the reply text — never English phonetics over Norwegian words). The
composition root logs the plan's fallbacks and threads it onto the turn context.
"""

from __future__ import annotations

from persona.language_capability import (
    CanonicalLanguage,
    CapabilityRegistry,
    LanguageCapability,
    Provider,
)
from persona_voice.agent.language import (
    CallLanguagePlan,
    apply_stt_route,
    apply_tts_route,
    maybe_apply_stt_route,
    maybe_apply_tts_route,
    resolve_call_languages,
    stt_is_utterance_level,
    tts_is_utterance_level,
)
from persona_voice.stt.config import StreamingSTTConfig
from persona_voice.tts.config import StreamingTTSConfig


def test_english_persona_plan_has_no_fallbacks() -> None:
    plan = resolve_call_languages("en")
    assert plan.stt.code == "en"
    assert plan.stt.model == "nova-3"
    assert plan.tts.code == "en"
    assert plan.reply_language == CanonicalLanguage.EN
    assert plan.fallbacks == ()


def test_norwegian_persona_routes_to_no_and_nova3() -> None:
    plan = resolve_call_languages("nb")  # declares Bokmål; collapses to no
    assert plan.stt.model == "nova-3"
    assert plan.stt.code == "no"
    assert plan.tts.code == "no"
    assert plan.reply_language == CanonicalLanguage.NO
    assert plan.fallbacks == ()


def test_unserved_language_falls_back_both_halves() -> None:
    plan = resolve_call_languages("klingon")
    assert plan.stt.code == "en"
    assert plan.tts.code == "en"
    assert plan.reply_language == CanonicalLanguage.EN
    # both STT and TTS report the fallback
    assert len(plan.fallbacks) == 2
    assert {f.provider for f in plan.fallbacks} == {Provider.DEEPGRAM, Provider.CARTESIA}


def test_reply_language_follows_tts_when_only_tts_falls_back() -> None:
    """If TTS cannot speak the declared language, the reply must be English too —
    so the user never hears English phonetics reading the declared language."""
    registry = CapabilityRegistry(
        stt={
            CanonicalLanguage.EN: LanguageCapability(
                canonical=CanonicalLanguage.EN,
                provider=Provider.DEEPGRAM,
                supported=True,
                code="en",
                model="nova-3",
            ),
            CanonicalLanguage.NO: LanguageCapability(
                canonical=CanonicalLanguage.NO,
                provider=Provider.DEEPGRAM,
                supported=True,
                code="no",
                model="nova-3",
            ),
        },
        tts={
            CanonicalLanguage.EN: LanguageCapability(
                canonical=CanonicalLanguage.EN,
                provider=Provider.CARTESIA,
                supported=True,
                code="en",
            ),
            # NO absent from TTS → TTS falls back to English.
        },
    )
    plan = resolve_call_languages("no", registry=registry)
    assert plan.stt.code == "no"  # STT still serves Norwegian
    assert plan.tts.code == "en"  # TTS fell back
    assert plan.reply_language == CanonicalLanguage.EN  # reply follows the voice
    assert len(plan.fallbacks) == 1
    assert plan.fallbacks[0].provider == Provider.CARTESIA


def test_plan_is_frozen() -> None:
    import pytest
    from pydantic import ValidationError

    plan = resolve_call_languages("en")
    with pytest.raises(ValidationError):
        plan.stt = plan.stt  # type: ignore[misc]


def test_isinstance_call_language_plan() -> None:
    assert isinstance(resolve_call_languages("en"), CallLanguagePlan)


# ---------- B3: applying the STT route to the Deepgram config ---------------


def test_apply_stt_route_pins_norwegian_to_nova3_and_no() -> None:
    base = StreamingSTTConfig(model="nova-3", language_hint="en")
    plan = resolve_call_languages("nb")
    pinned = apply_stt_route(base, plan.stt)
    assert pinned.model == "nova-3"
    assert pinned.language_hint == "no"  # NOT nb, NOT the global en hint
    # The base config is unchanged (model_copy, not mutation).
    assert base.language_hint == "en"


def test_apply_stt_route_preserves_other_config_fields() -> None:
    base = StreamingSTTConfig(model="nova-3", language_hint="en", deepgram_endpointing_ms=250)
    pinned = apply_stt_route(base, resolve_call_languages("en").stt)
    assert pinned.deepgram_endpointing_ms == 250
    assert pinned.language_hint == "en"


# ---------- B4: applying the TTS route to the Cartesia config ---------------


def test_apply_tts_route_sets_norwegian_language_code() -> None:
    base = StreamingTTSConfig(provider="cartesia")
    pinned = apply_tts_route(base, resolve_call_languages("nb").tts)
    assert pinned.language == "no"
    assert base.language is None  # copy, not mutation


def test_apply_tts_route_english_sets_en() -> None:
    pinned = apply_tts_route(
        StreamingTTSConfig(provider="cartesia"), resolve_call_languages("en").tts
    )
    assert pinned.language == "en"


# ---------- Spec V14 D-V14-5: utterance-level providers skip the route ------


def test_stt_is_utterance_level_predicate() -> None:
    assert stt_is_utterance_level("gladia") is True
    assert stt_is_utterance_level("deepgram") is False
    assert stt_is_utterance_level("speechmatics") is False


def test_tts_is_utterance_level_predicate() -> None:
    assert tts_is_utterance_level("elevenlabs") is True
    assert tts_is_utterance_level("cartesia") is False


def test_maybe_apply_stt_route_skips_for_gladia() -> None:
    """Gladia code-switches from the audio — the route is NOT applied (the config
    is returned untouched, no language pinned). D-V14-5, direction 1."""
    base = StreamingSTTConfig(provider="gladia", gladia_api_key="k", language_hint="en")
    result = maybe_apply_stt_route(base, resolve_call_languages("nb").stt)
    assert result is base  # untouched
    assert result.language_hint == "en"  # NOT re-narrowed to "no"


def test_maybe_apply_stt_route_pins_for_deepgram_byte_identical() -> None:
    """Deepgram (incumbent) is pinned exactly as apply_stt_route would — the
    byte-identical incumbent path. D-V14-5, direction 2."""
    base = StreamingSTTConfig(provider="deepgram", model="nova-3", language_hint="en")
    plan = resolve_call_languages("nb")
    via_maybe = maybe_apply_stt_route(base, plan.stt)
    via_direct = apply_stt_route(base, plan.stt)
    assert via_maybe.language_hint == via_direct.language_hint == "no"
    assert via_maybe.model == via_direct.model


def test_maybe_apply_tts_route_skips_for_elevenlabs() -> None:
    """ElevenLabs auto-follows the reply text — the route is NOT applied. D-V14-5."""
    base = StreamingTTSConfig(provider="elevenlabs", elevenlabs_api_key="k")
    result = maybe_apply_tts_route(base, resolve_call_languages("nb").tts)
    assert result is base
    assert result.language is None  # NOT pinned to "no"


def test_maybe_apply_tts_route_pins_for_cartesia_byte_identical() -> None:
    """Cartesia (incumbent) is pinned exactly as apply_tts_route would. D-V14-5."""
    base = StreamingTTSConfig(provider="cartesia")
    plan = resolve_call_languages("nb")
    via_maybe = maybe_apply_tts_route(base, plan.tts)
    via_direct = apply_tts_route(base, plan.tts)
    assert via_maybe.language == via_direct.language == "no"
