"""Spec V12 T2 — the versioned warm-subset expressivity map (V12-D-3/D-6).

A pure, frozen data asset mapping N5's feeling-tags → Cartesia Sonic-3.5
``generation_config`` expressivity, bounded by restraint (warm subset only, small
speed/volume deltas, stoic/no-tag → None). The map values are provisional — ratified
by the real-voice operator pass (V12-D-7); CI pins the SHAPE:

* full coverage of N5's vocabulary (no tag silently unmapped — the drift guard);
* the exact V12-D-3 profile values + the caring-not-anxious ``concerned/worried`` call;
* the restraint ceiling (no value exceeds the small-delta bound);
* SDK-Literal validation (V12-D-6): every emotion string is a real Cartesia emotion,
  so a provider rename breaks CI, not production;
* first-recognized-tag-wins per utterance (V12-D-2 lead-with-the-tag);
* the stable-base / Beta-additive layering (emotion drops independently of speed/volume).
"""

from __future__ import annotations

import typing

import pytest
from persona_runtime.emotional.vocabulary import FEELING_TAGS
from persona_voice.model.expressivity import (
    EXPRESSIVITY_MAP,
    V12_EXPRESSIVITY_VERSION,
    VoiceExpressivity,
    resolve_expressivity,
)
from pydantic import ValidationError


def test_version_constant_is_set() -> None:
    assert V12_EXPRESSIVITY_VERSION == "v1"  # versioned artifact (Spec 10 discipline)


def test_map_covers_every_n5_feeling_tag() -> None:
    """Drift guard: every N5 tag maps to an expressivity — none silently unmapped."""
    assert set(EXPRESSIVITY_MAP) == set(FEELING_TAGS)


@pytest.mark.parametrize(
    ("tag", "emotion", "speed", "volume"),
    [
        # BRIGHT profile
        ("joyful", "happy", 1.06, 1.04),
        ("excited", "excited", 1.06, 1.04),
        ("amazed", "amazed", 1.06, 1.04),
        # WARM profile
        ("happy", "happy", 1.00, 1.00),
        ("proud_of_you", "proud", 1.00, 1.00),
        ("affectionate", "affectionate", 1.00, 1.00),
        ("supportive", "confident", 1.00, 1.00),
        # TENDER profile: caring, not anxious
        ("sympathetic", "sympathetic", 0.97, 0.98),
        ("concerned", "sympathetic", 0.97, 0.98),
        ("worried", "sympathetic", 0.97, 0.98),
        ("thoughtful", "contemplative", 0.97, 0.98),
        # SUBDUED profile
        ("sad", "sad", 0.95, 0.96),
        ("wistful", "wistful", 0.95, 0.96),
    ],
)
def test_v12_d3_exact_values(tag: str, emotion: str, speed: float, volume: float) -> None:
    expr = EXPRESSIVITY_MAP[tag]
    assert expr == VoiceExpressivity(emotion=emotion, speed=speed, volume=volume)


def test_concerned_and_worried_are_caring_not_anxious() -> None:
    """A persona voicing concern sounds supportive, never anxious/scared (V12-D-3)."""
    for tag in ("concerned", "worried"):
        assert EXPRESSIVITY_MAP[tag].emotion == "sympathetic"
        assert EXPRESSIVITY_MAP[tag].emotion not in {"anxious", "scared", "panicked", "alarmed"}


def test_restraint_ceiling_all_deltas_are_small() -> None:
    """Restraint by construction: no profile exceeds the small-delta bound (V12-D-3)."""
    for expr in EXPRESSIVITY_MAP.values():
        assert expr.speed is not None
        assert 0.94 <= expr.speed <= 1.07
        assert expr.volume is not None
        assert 0.95 <= expr.volume <= 1.05


def _sdk_emotion_literal() -> set[str]:
    """The allowed Cartesia emotion strings, read from the installed SDK (V12-D-6)."""
    from cartesia.types.generation_config_param import GenerationConfigParam

    hints = typing.get_type_hints(GenerationConfigParam)
    emotion_ann = hints["emotion"]  # Union[str, Literal[...]]
    for arg in typing.get_args(emotion_ann):
        members = typing.get_args(arg)
        if members and all(isinstance(m, str) for m in members):
            return set(members)
    raise AssertionError("could not locate the emotion Literal in the Cartesia SDK")


def test_every_mapped_emotion_is_a_real_sdk_emotion() -> None:
    """Build-time drift guard: a Cartesia emotion rename breaks CI, not production."""
    allowed = _sdk_emotion_literal()
    used = {e.emotion for e in EXPRESSIVITY_MAP.values() if e.emotion is not None}
    assert used <= allowed, f"unknown-to-SDK emotions: {used - allowed}"


def test_mapped_emotions_use_only_the_warm_subset() -> None:
    """The map never targets a hostile emotion (warm-subset only, V12-D-3)."""
    hostile = {
        "angry",
        "mad",
        "outraged",
        "frustrated",
        "agitated",
        "threatened",
        "disgusted",
        "contempt",
        "envious",
        "sarcastic",
        "ironic",
    }
    used = {e.emotion for e in EXPRESSIVITY_MAP.values() if e.emotion is not None}
    assert used.isdisjoint(hostile)


class TestResolve:
    def test_no_tags_is_none(self) -> None:
        """No stance tag ⇒ None ⇒ today's clean flat read."""
        assert resolve_expressivity([]) is None

    def test_first_recognized_tag_wins(self) -> None:
        """Lead-with-the-tag (V12-D-2): two tags in one utterance ⇒ the first, no ambiguity."""
        assert resolve_expressivity(["excited", "sad"]) == EXPRESSIVITY_MAP["excited"]

    def test_unmapped_tag_is_skipped_not_crashed(self) -> None:
        """Robust to N5 adding a tag V12 hasn't mapped: skip to the next recognized one."""
        assert resolve_expressivity(["not_a_tag", "happy"]) == EXPRESSIVITY_MAP["happy"]

    def test_no_recognized_tag_is_none(self) -> None:
        assert resolve_expressivity(["not_a_tag", "also_bogus"]) is None


class TestGenerationConfig:
    def test_omits_none_fields(self) -> None:
        expr = VoiceExpressivity(emotion="happy", speed=1.06, volume=1.04)
        assert expr.to_generation_config() == {"emotion": "happy", "speed": 1.06, "volume": 1.04}

    def test_beta_emotion_drops_independently_of_stable_speed_volume(self) -> None:
        """V12-D-6 layering: include_emotion=False keeps the stable base, drops Beta emotion."""
        expr = EXPRESSIVITY_MAP["excited"]
        assert expr.to_generation_config(include_emotion=False) == {"speed": 1.06, "volume": 1.04}
        assert "emotion" not in expr.to_generation_config(include_emotion=False)

    def test_warm_profile_degrades_to_neutral_when_emotion_dropped(self) -> None:
        """A WARM tag with emotion dropped is effectively flat (speed/volume 1.0)."""
        cfg = EXPRESSIVITY_MAP["happy"].to_generation_config(include_emotion=False)
        assert cfg == {"speed": 1.00, "volume": 1.00}


class TestConstruction:
    def test_frozen(self) -> None:
        expr = VoiceExpressivity(emotion="happy", speed=1.0, volume=1.0)
        with pytest.raises(ValidationError):
            expr.emotion = "sad"  # type: ignore[misc]

    def test_speed_out_of_cartesia_range_rejected(self) -> None:
        with pytest.raises(ValidationError):
            VoiceExpressivity(speed=2.0)  # Cartesia valid range is [0.6, 1.5]

    def test_volume_out_of_cartesia_range_rejected(self) -> None:
        with pytest.raises(ValidationError):
            VoiceExpressivity(volume=3.0)  # Cartesia valid range is [0.5, 2.0]

    def test_all_none_is_empty_config(self) -> None:
        assert VoiceExpressivity().to_generation_config() == {}
