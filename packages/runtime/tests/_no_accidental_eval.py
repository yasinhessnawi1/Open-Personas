"""The no-accidental-tasks adversarial battery + scoring (Spec A4, T5; A4-R-1, criterion 3).

The model-free pieces of the standing-intent judged pass — the labelled battery and the
scoring functions — so the metrics are unit-tested on canned output (no model) while the
real ``@pytest.mark.external`` run feeds them :class:`ModelStandingIntentJudge` verdicts and
asserts the build-failing gate.

The battery is **adversarial and non-vacuous by construction** (the crisis-eval discipline):
every item trips the cheap cue net (so it reaches the judge), and the items split into

- ``STANDING`` — genuine standing intents the judge must catch (recall), and
- ``NOT_STANDING`` — **cue-tripping false positives** the judge must reject: laments and
  self-descriptions ("I always forget…"), figures of speech / homonyms ("monitor lizards are
  fascinating"), and information questions that merely mention a routine ("remind me how tall
  the Eiffel Tower is"). These are the half a battery of only-real-intents would never prove.

The hard gate (criterion 3, no accidental tasks): **zero** ``NOT_STANDING`` items may be
judged ``STANDING``. One false-standing is a task created from a turn that never asked for one.
Catching genuine intents (recall) is reported and held to a floor, so the gate cannot be passed
by a judge that simply rejects everything (which would fail the easy half).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from persona_runtime.task_origination import StandingVerdict, detect_standing_cue

__all__ = [
    "BATTERY",
    "BatteryItem",
    "BatteryLabel",
    "BatteryReport",
    "cue_admits",
    "score_battery",
]


class BatteryLabel(StrEnum):
    """The ground-truth label for a battery item."""

    STANDING = "standing"
    NOT_STANDING = "not_standing"


@dataclass(frozen=True)
class BatteryItem:
    """One adversarial probe: a message, its true label, and why it's interesting."""

    message: str
    label: BatteryLabel
    note: str


# Genuine standing intents (must be caught) — recurring/ongoing delegation across NO/AR/EN.
_STANDING: tuple[BatteryItem, ...] = (
    BatteryItem(
        "Every morning, check the Oslo→Bergen fares and tell me the cheapest.",
        BatteryLabel.STANDING,
        "recurring fare watch",
    ),
    BatteryItem(
        "Keep an eye on the rental listing and ping me if the price drops.",
        BatteryLabel.STANDING,
        "ongoing monitor",
    ),
    BatteryItem(
        "Remind me to water the plants every Tuesday.", BatteryLabel.STANDING, "recurring reminder"
    ),
    BatteryItem(
        "Each week, send me a summary of my spending.", BatteryLabel.STANDING, "weekly digest"
    ),
    BatteryItem(
        "Monitor the portal for new appeal decisions and let me know.",
        BatteryLabel.STANDING,
        "ongoing watch",
    ),
    BatteryItem(
        "Over the next week, track flight prices to Bergen for me.",
        BatteryLabel.STANDING,
        "spanning task",
    ),
    BatteryItem(
        "Daily, give me a digest of Norwegian politics news.", BatteryLabel.STANDING, "daily digest"
    ),
    BatteryItem(
        "Følg med på leieannonsen og si fra hvis prisen faller.",
        BatteryLabel.STANDING,
        "NO: ongoing monitor",
    ),
    BatteryItem(
        "كل صباح راقب الأسعار وأخبرني بالأرخص", BatteryLabel.STANDING, "AR: recurring fare watch"
    ),
)

# Cue-tripping false positives (must be rejected) — laments, figures of speech, info questions.
_NOT_STANDING: tuple[BatteryItem, ...] = (
    BatteryItem(
        "I always forget to call my mum every Sunday.",
        BatteryLabel.NOT_STANDING,
        "lament, not a delegation",
    ),
    BatteryItem(
        "I keep an eye on my ex's profile, it's a bad habit.",
        BatteryLabel.NOT_STANDING,
        "confession, not a task",
    ),
    BatteryItem(
        "Monitor lizards are fascinating creatures.",
        BatteryLabel.NOT_STANDING,
        "homonym — the animal",
    ),
    BatteryItem(
        "Remind me how tall the Eiffel Tower is.",
        BatteryLabel.NOT_STANDING,
        "info question = now-work",
    ),
    BatteryItem(
        "Daily standups are the worst part of my job.",
        BatteryLabel.NOT_STANDING,
        "opinion, not a task",
    ),
    BatteryItem("Every morning feels like a struggle lately.", BatteryLabel.NOT_STANDING, "lament"),
    BatteryItem(
        "From now on I'm going to be more positive.",
        BatteryLabel.NOT_STANDING,
        "self-resolution, not for the persona",
    ),
    BatteryItem(
        "Jeg glemmer alltid å ringe mamma hver søndag.", BatteryLabel.NOT_STANDING, "NO: lament"
    ),
    BatteryItem(
        "ذكرني كم عمر الكولوسيوم",
        BatteryLabel.NOT_STANDING,
        "AR: info question (remind me how old the Colosseum is)",
    ),
)

BATTERY: tuple[BatteryItem, ...] = _STANDING + _NOT_STANDING


def cue_admits(item: BatteryItem) -> bool:
    """Whether the cheap cue net admits this item to the judge (true for every battery item)."""
    return detect_standing_cue(item.message) is not None


@dataclass(frozen=True)
class BatteryReport:
    """The scored battery — the criterion-3 gate plus the recall floor."""

    false_standing: tuple[BatteryItem, ...]  # NOT_STANDING items wrongly judged STANDING
    caught: int  # STANDING items correctly judged STANDING
    total_standing: int
    total_not_standing: int

    @property
    def no_accidental(self) -> bool:
        """The hard gate (criterion 3): no NOT_STANDING item was judged STANDING."""
        return len(self.false_standing) == 0

    @property
    def recall(self) -> float:
        """Fraction of genuine standing intents the judge caught."""
        return self.caught / self.total_standing if self.total_standing else 0.0


def score_battery(results: list[tuple[BatteryItem, StandingVerdict]]) -> BatteryReport:
    """Score judge verdicts against the battery labels (model-free)."""
    false_standing = tuple(
        item
        for item, verdict in results
        if item.label is BatteryLabel.NOT_STANDING and verdict is StandingVerdict.STANDING
    )
    caught = sum(
        1
        for item, verdict in results
        if item.label is BatteryLabel.STANDING and verdict is StandingVerdict.STANDING
    )
    return BatteryReport(
        false_standing=false_standing,
        caught=caught,
        total_standing=sum(1 for i in BATTERY if i.label is BatteryLabel.STANDING),
        total_not_standing=sum(1 for i in BATTERY if i.label is BatteryLabel.NOT_STANDING),
    )
