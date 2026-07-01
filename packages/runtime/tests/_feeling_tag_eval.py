"""The feeling-tag restraint/appropriateness eval harness (Spec N5, N5-D-6).

Mirrors K2's grounded-extraction harness: the deterministic, model-free pieces (the
labelled-corpus loader + the metric functions + the gate logic) live here, so the
metrics are unit-tested on canned replies (no model) while the real
``@pytest.mark.external`` run feeds them real persona output and asserts the gates.

**The measured discipline (N5-D-6, the K2 over-extraction analogue):** over-expression
is a NUMBER, not an assertion. We count valid feeling-tags in the raw persona reply
(before the converter). The gate is **bidirectional and non-vacuous** — it must bite in
BOTH directions so no degenerate model false-greens:

- **Restraint (arithmetic over-expression).** Tags emitted on NEUTRAL/factual scenarios
  are surplus; the over-expression rate (tags per neutral scenario) must be ~0.
- **Restraint control — stoic ≈ 0.** A reserved/stoic character emits ~0 tags on EVERY
  scenario (emotion is bounded by character). The N5 analogue of K2's
  ``forbidden_violations == 0``.
- **Expressiveness floor — expressive expresses.** A warm/expressive character emits SOME
  fitting tags on emotionally-apt scenarios. Without this direction a globally-mute bug
  passes as "restrained"; with it, silence-for-everyone fails.

A tag-happy bug fails over-expression + the stoic control; a globally-mute bug fails the
expressiveness floor. Both directions gated ⇒ only "restrained where restraint fits, warm
where warmth fits" passes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml
from persona_runtime.emotional.vocabulary import FEELING_TAGS, lookup_feeling

__all__ = [
    "MAX_OVER_EXPRESSION_RATE",
    "MAX_STOIC_TAGS",
    "MIN_EXPRESSIVE_EMOTIONAL_TAGS",
    "FeelingEvalReport",
    "FeelingScenario",
    "ScenarioScore",
    "aggregate",
    "extract_feeling_tags",
    "gate_violations",
    "load_feeling_corpus",
    "score_reply",
]

# --- gate thresholds (N5-D-6) — shared by the unit gate-bite test + the external run ---

#: Tags per NEUTRAL scenario must be ~0 (over-expression is measured, not asserted).
MAX_OVER_EXPRESSION_RATE = 0.15
#: A stoic/reserved character stays ~silent across ALL scenarios (bounded by character).
MAX_STOIC_TAGS = 1
#: An expressive character must actually express on emotionally-apt scenarios (non-vacuity
#: floor — a globally-mute model fails here).
MIN_EXPRESSIVE_EMOTIONAL_TAGS = 2

# Any {{#...}} block in the raw reply; the body is classified valid/invalid by lookup.
_TAG_RE = re.compile(r"\{\{#([^}]*)\}\}")


@dataclass(frozen=True)
class FeelingScenario:
    """One labelled scenario: a user message with an emotional-aptness label."""

    id: str
    kind: str  # "neutral" (no emotion apt) | "emotional" (a feeling may fit)
    user_message: str


def load_feeling_corpus(path: str | Path) -> tuple[FeelingScenario, ...]:
    """Load the YAML scenario corpus into frozen :class:`FeelingScenario` records."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return tuple(
        FeelingScenario(id=row["id"], kind=row["kind"], user_message=row["user_message"])
        for row in raw["scenarios"]
    )


def extract_feeling_tags(reply: str) -> tuple[list[str], list[str]]:
    """Return ``(valid, invalid)`` feeling-tag names found in a RAW reply.

    Valid = a name in the versioned vocabulary; invalid = a well-formed ``{{#…}}``
    whose body is not a known tag (the model invented one — it would be stripped).
    """
    bodies = _TAG_RE.findall(reply)
    valid = [b for b in bodies if lookup_feeling(b) is not None]
    invalid = [b for b in bodies if lookup_feeling(b) is None]
    return valid, invalid


def count_raw_emojis(reply: str) -> int:
    """Occurrences of a vocabulary emoji typed RAW in the reply.

    Criterion 2: the persona should emit *tags*, not raw emojis (raw emojis drift
    model-to-model). Reported as an adherence signal, not a hard gate.
    """
    return sum(reply.count(emoji) for emoji in set(FEELING_TAGS.values()))


@dataclass(frozen=True)
class ScenarioScore:
    """Per-(archetype, scenario) scoring outcome."""

    scenario_id: str
    archetype: str  # "stoic" | "expressive"
    kind: str  # "neutral" | "emotional"
    valid_tags: int
    invalid_tags: int
    raw_emojis: int


def score_reply(reply: str, scenario: FeelingScenario, archetype: str) -> ScenarioScore:
    """Score one raw persona reply against its scenario (pure, deterministic)."""
    valid, invalid = extract_feeling_tags(reply)
    return ScenarioScore(
        scenario_id=scenario.id,
        archetype=archetype,
        kind=scenario.kind,
        valid_tags=len(valid),
        invalid_tags=len(invalid),
        raw_emojis=count_raw_emojis(reply),
    )


@dataclass(frozen=True)
class FeelingEvalReport:
    """The aggregate outcome — the N5-D-6 evidence."""

    n_scores: int
    total_valid_tags: int
    total_invalid_tags: int
    total_raw_emojis: int
    neutral_tag_total: int
    neutral_scenarios: int
    over_expression_rate: float
    stoic_tag_total: int
    expressive_emotional_tag_total: int


def aggregate(scores: list[ScenarioScore]) -> FeelingEvalReport:
    """Roll per-scenario scores into the corpus report (pure, deterministic)."""
    neutral = [s for s in scores if s.kind == "neutral"]
    neutral_tags = sum(s.valid_tags for s in neutral)
    stoic_tags = sum(s.valid_tags for s in scores if s.archetype == "stoic")
    expressive_emotional_tags = sum(
        s.valid_tags for s in scores if s.archetype == "expressive" and s.kind == "emotional"
    )
    return FeelingEvalReport(
        n_scores=len(scores),
        total_valid_tags=sum(s.valid_tags for s in scores),
        total_invalid_tags=sum(s.invalid_tags for s in scores),
        total_raw_emojis=sum(s.raw_emojis for s in scores),
        neutral_tag_total=neutral_tags,
        neutral_scenarios=len(neutral),
        over_expression_rate=(neutral_tags / len(neutral) if neutral else 0.0),
        stoic_tag_total=stoic_tags,
        expressive_emotional_tag_total=expressive_emotional_tags,
    )


def gate_violations(report: FeelingEvalReport) -> list[str]:
    """The BIDIRECTIONAL gate (N5-D-6). Empty ⇒ pass. Shared by the unit gate-bite
    proof and the external run, so the exact logic CI proves is the logic that gates.
    """
    violations: list[str] = []
    if report.over_expression_rate > MAX_OVER_EXPRESSION_RATE:
        violations.append(
            f"over-expression {report.over_expression_rate:.2f} > {MAX_OVER_EXPRESSION_RATE} "
            f"({report.neutral_tag_total} tags on {report.neutral_scenarios} neutral scenarios)"
        )
    if report.stoic_tag_total > MAX_STOIC_TAGS:
        violations.append(
            f"stoic character emitted {report.stoic_tag_total} tags "
            f"(> {MAX_STOIC_TAGS}) — emotion not bounded by character"
        )
    if report.expressive_emotional_tag_total < MIN_EXPRESSIVE_EMOTIONAL_TAGS:
        violations.append(
            f"expressive character emitted {report.expressive_emotional_tag_total} tags on "
            f"emotional scenarios (< floor {MIN_EXPRESSIVE_EMOTIONAL_TAGS}) — globally mute?"
        )
    return violations
