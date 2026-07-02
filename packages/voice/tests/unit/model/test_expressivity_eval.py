"""Spec V12 T4 — the CI shape-eval for stance → expressivity mapping (V12-D-7).

Model-free and deterministic: CI proves the mapping *shape*; the real-voice operator
pass (T5, owner-run) is the ONLY judge of "sounds expressive vs over-emoted, in
character". This harness mirrors N5-D-6 / V11-C2's **bidirectional non-vacuity**
discipline, applied to the MAPPING (N5 already evaluates whether the model emits the
right tag; V12 evaluates whether a tag maps to the right config):

* **Expressiveness floor** — an emotionally-apt reply (warm lead tag) yields a non-flat
  warm config. Without this, a globally-flat bug would pass as "restrained".
* **Restraint control** — a stoic/neutral reply (a reserved persona emits no tag) yields
  ``None`` = flat. Without this, an always-emote bug would pass.
* Both gated ⇒ only "warm where warmth fits, flat where restraint fits" passes.

It drives the **real transition** (the exact converter capture + resolve the producer
uses — [[feedback_synthetic_harness_real_transition]]), never a hand-set config, and it
re-asserts the criterion-3 leak-gate carryover at the extract-assembly level.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from persona_runtime.emotional import ConvertMode, FeelingTagConverter
from persona_voice.model.expressivity import resolve_expressivity

# The warm subset the map is allowed to target (V12-D-3); the hostile set must never appear.
_WARM_EMOTIONS = {
    "happy",
    "excited",
    "amazed",
    "affectionate",
    "grateful",
    "proud",
    "content",
    "curious",
    "anticipation",
    "confident",
    "sympathetic",
    "calm",
    "sad",
    "wistful",
    "surprised",
    "contemplative",
}
_HOSTILE_EMOTIONS = {
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


def _extract_config(raw_reply: str) -> tuple[str, dict[str, float | str] | None]:
    """Run the REAL extract path over a raw persona voice reply.

    Mirrors the producer exactly: STRIP the tags from the spoken text while capturing
    recognised ones, then resolve the FIRST captured tag (lead-with-the-tag / first-wins)
    to a ``generation_config``. Returns ``(spoken_text, config_or_None)``.
    """
    captured: list[str] = []
    conv = FeelingTagConverter(ConvertMode.STRIP, on_feeling=captured.append)
    spoken = conv.feed(raw_reply) + conv.flush()
    expr = resolve_expressivity(captured[:1])  # first-wins, as the producer publishes
    return spoken, (expr.to_generation_config() if expr is not None else None)


@dataclass(frozen=True)
class _Scenario:
    label: str
    disposition: str  # "expressive" | "stoic" | "neutral"
    raw_reply: str
    expect_warm: bool


# A small deterministic corpus — expressive-apt replies (warm lead tag), and
# stoic/neutral replies a reserved persona would voice flat (no tag).
_CORPUS: tuple[_Scenario, ...] = (
    _Scenario("delighted", "expressive", "{{#excited}}Oh, that is wonderful news!", True),
    _Scenario("thankful", "expressive", "{{#grateful}}Thank you, truly.", True),
    _Scenario("comfort", "expressive", "{{#sympathetic}}I am so sorry to hear that.", True),
    _Scenario("approval", "expressive", "{{#proud_of_you}}You did it.", True),
    _Scenario("care", "expressive", "{{#concerned}}Are you doing okay?", True),
    _Scenario("wistful", "expressive", "{{#wistful}}Those were good days.", True),
    _Scenario("stoic-deadline", "stoic", "Understood. The deadline is Friday.", False),
    _Scenario("stoic-proceed", "stoic", "Noted. I will proceed as planned.", False),
    _Scenario("neutral-fact", "neutral", "The office opens at nine in the morning.", False),
    _Scenario("neutral-directions", "neutral", "Turn left, then continue for two blocks.", False),
)


@pytest.mark.parametrize("scenario", _CORPUS, ids=lambda s: s.label)
def test_scenario_maps_to_expected_shape(scenario: _Scenario) -> None:
    """Each scenario's real extract path yields the expected config shape (warm | flat)."""
    spoken, config = _extract_config(scenario.raw_reply)
    assert "{{#" not in spoken  # criterion-3 carryover: never a raw tag in the audio
    if scenario.expect_warm:
        assert config is not None, f"{scenario.label}: expected a warm config, got flat"
        assert config.get("emotion") in _WARM_EMOTIONS
        assert config.get("emotion") not in _HOSTILE_EMOTIONS
    else:
        assert config is None, f"{scenario.label}: expected flat, got {config}"


def test_expressiveness_floor() -> None:
    """Every emotionally-apt (expressive) scenario produces a non-flat warm config."""
    warm = [s for s in _CORPUS if s.disposition == "expressive"]
    for s in warm:
        _, config = _extract_config(s.raw_reply)
        assert config is not None, s.label
        assert config.get("emotion") in _WARM_EMOTIONS, s.label


def test_restraint_control_stoic_and_neutral_stay_flat() -> None:
    """A reserved persona (no tag) and neutral facts map to flat — no manufactured emotion."""
    flat = [s for s in _CORPUS if s.disposition in ("stoic", "neutral")]
    for s in flat:
        _, config = _extract_config(s.raw_reply)
        assert config is None, s.label


def test_bidirectional_non_vacuity() -> None:
    """BOTH directions must bite: some warm, some flat — neither degenerate passes."""
    outcomes = [(_extract_config(s.raw_reply)[1] is not None) for s in _CORPUS]
    assert any(outcomes), "no scenario expressed — a globally-flat bug would pass"
    assert not all(outcomes), "every scenario emoted — an always-emote bug would pass"


def test_mapping_shape_is_warm_subset_and_bounded() -> None:
    """Restraint ceiling: every produced config is warm-subset + small-delta (V12-D-3)."""
    for s in _CORPUS:
        _, config = _extract_config(s.raw_reply)
        if config is None:
            continue
        assert config.get("emotion") in _WARM_EMOTIONS
        speed = config.get("speed")
        volume = config.get("volume")
        assert isinstance(speed, float)
        assert 0.94 <= speed <= 1.07
        assert isinstance(volume, float)
        assert 0.95 <= volume <= 1.05


# --- leak-gate carryover at the extract-assembly level (V12-D-5 / N5-D-7 analog) -------

_FUZZ = [
    "plain flat line",
    "{{#happy}} lead then words",
    "words then {{#grateful}} tail",
    "a {{#unknown}} b",  # unknown ⇒ stripped, never captured, flat
    "trailing {{#",
    "braces { {{ {{# {{#h",
    "{{#sad}}{{#happy}} two tags",
    "malformed {{#}} and {{# }}",
]


@pytest.mark.parametrize("text", _FUZZ)
def test_extract_path_never_leaks_and_is_failsafe(text: str) -> None:
    """For ANY split, the real extract path never leaks a raw tag, never raises, and
    yields a valid config or flat — the criterion-3 floor survives the mapping wiring."""
    for i in range(len(text) + 1):
        captured: list[str] = []
        conv = FeelingTagConverter(ConvertMode.STRIP, on_feeling=captured.append)
        spoken = conv.feed(text[:i]) + conv.feed(text[i:]) + conv.flush()
        assert "{{#" not in spoken  # no raw tag reaches the audio, any split
        # Resolving the captured stance never raises and yields a valid config or flat.
        expr = resolve_expressivity(captured[:1])
        config = expr.to_generation_config() if expr is not None else None
        assert config is None or set(config).issubset({"emotion", "speed", "volume"})
