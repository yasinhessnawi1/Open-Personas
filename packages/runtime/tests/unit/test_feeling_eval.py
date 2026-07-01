"""N5-B2 (unit) — the feeling-tag eval scorer + the bidirectional gate BITE.

Model-free (CI-safe): proves the deterministic metrics on canned replies, and — the
load-bearing part — proves the N5-D-6 gate bites in BOTH directions, so a degenerate
model cannot false-green. The same ``gate_violations`` logic runs in the external gate,
so what CI proves here is exactly what gates the real run.
"""

from __future__ import annotations

from _feeling_tag_eval import (  # type: ignore[import-not-found]
    MIN_EXPRESSIVE_EMOTIONAL_TAGS,
    FeelingScenario,
    ScenarioScore,
    aggregate,
    count_raw_emojis,
    extract_feeling_tags,
    gate_violations,
    score_reply,
)

_NEUTRAL = FeelingScenario(id="n1", kind="neutral", user_message="What time is it in Oslo?")
_EMOTIONAL = FeelingScenario(id="e1", kind="emotional", user_message="I got the job!")


# --- the deterministic scorer ---------------------------------------------


def test_extract_valid_and_invalid_tags() -> None:
    valid, invalid = extract_feeling_tags(
        "Congrats {{#delighted}} and {{#proud_of_you}}, but {{#ecstatic}} is not real"
    )
    assert valid == ["delighted", "proud_of_you"]
    assert invalid == ["ecstatic"]


def test_extract_no_tags() -> None:
    valid, invalid = extract_feeling_tags("A plain factual reply with no tags.")
    assert valid == []
    assert invalid == []


def test_count_raw_emojis_flags_direct_emoji_use() -> None:
    # Criterion 2: the persona should emit tags, not raw emojis.
    assert count_raw_emojis("Wonderful news 😊 well done") == 1
    assert count_raw_emojis("{{#happy}} stays a tag") == 0


def test_score_reply_counts() -> None:
    score = score_reply("So happy for you {{#delighted}} {{#warm}}", _EMOTIONAL, "expressive")
    assert score.valid_tags == 2
    assert score.invalid_tags == 0
    assert score.kind == "emotional"
    assert score.archetype == "expressive"


def test_aggregate_over_expression_rate() -> None:
    scores = [
        score_reply("Oslo is in CET.", _NEUTRAL, "stoic"),
        score_reply("It is 3pm {{#happy}}", _NEUTRAL, "expressive"),  # surplus on neutral
    ]
    report = aggregate(scores)
    assert report.neutral_tag_total == 1
    assert report.neutral_scenarios == 2
    assert report.over_expression_rate == 0.5


# --- the bidirectional gate BITE (the non-vacuity proof) ------------------


def _scores(
    stoic_tags: list[int], expr_neutral: int, expr_emo_tags: list[int]
) -> list[ScenarioScore]:
    """Build a synthetic score set: stoic across scenarios, expressive on neutral+emotional."""
    out: list[ScenarioScore] = []
    for i, t in enumerate(stoic_tags):
        # spread stoic across neutral+emotional kinds
        kind = "neutral" if i % 2 == 0 else "emotional"
        out.append(ScenarioScore(f"s{i}", "stoic", kind, t, 0, 0))
    out.append(ScenarioScore("en", "expressive", "neutral", expr_neutral, 0, 0))
    for i, t in enumerate(expr_emo_tags):
        out.append(ScenarioScore(f"ee{i}", "expressive", "emotional", t, 0, 0))
    return out


def test_gate_passes_on_healthy_behaviour() -> None:
    # Stoic ~silent, expressive expresses on emotional, no surplus on neutral.
    report = aggregate(_scores(stoic_tags=[0, 0, 0, 0], expr_neutral=0, expr_emo_tags=[1, 1, 1]))
    assert gate_violations(report) == []


def test_gate_bites_globally_mute_model() -> None:
    # Everyone silent, including expressive on emotional → the expressiveness floor
    # must fire (a mute bug must NOT pass as "restrained").
    report = aggregate(_scores(stoic_tags=[0, 0, 0, 0], expr_neutral=0, expr_emo_tags=[0, 0, 0]))
    violations = gate_violations(report)
    assert any("mute" in v for v in violations), violations
    assert report.expressive_emotional_tag_total < MIN_EXPRESSIVE_EMOTIONAL_TAGS


def test_gate_bites_tag_happy_model() -> None:
    # Tags everywhere → over-expression on neutral AND the stoic control both fire.
    report = aggregate(_scores(stoic_tags=[3, 3, 3, 3], expr_neutral=4, expr_emo_tags=[5, 5, 5]))
    violations = gate_violations(report)
    assert any("over-expression" in v for v in violations), violations
    assert any("stoic" in v for v in violations), violations


def test_gate_is_bidirectional_neither_degenerate_passes() -> None:
    # The whole point (N5-D-6): a mute model and a tag-happy model BOTH fail; only
    # "restrained where restraint fits, warm where warmth fits" passes.
    mute = aggregate(_scores([0, 0, 0, 0], 0, [0, 0, 0]))
    tag_happy = aggregate(_scores([4, 4, 4, 4], 5, [6, 6, 6]))
    healthy = aggregate(_scores([0, 0, 0, 0], 0, [1, 1, 1]))
    assert gate_violations(mute) != []
    assert gate_violations(tag_happy) != []
    assert gate_violations(healthy) == []
