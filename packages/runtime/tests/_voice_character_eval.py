"""The voice-style + character-adherence eval harness (Spec V11, C1 / V11-D-8).

V11's analogue of the V4 "feels-natural" tradition and the K2/A2 judged gates: the
**measured** proof that a persona's voice answer is voice-styled and measurably
distinct from its chat answer (criterion 1), mirrors the user's formality
(criterion 2), inhabits its character (criterion 3, non-adversarial), carries no
"AI tell" punctuation (criterion 4), AND is not flattened by the style rules
(the evaluate-don't-accrete guard).

Like the continuation eval, the **model-free pieces live here** — the committed
scenario loader, the deterministic criterion-4 style check, the rubric→verdict
logic, the self-consistency vote, and the gate — so they are unit-tested on canned
judge output (no model), while the real LLM judge runs ``@pytest.mark.external``
from a DIFFERENT model family than the persona (self-enhancement bias).

The **persona-flattening dimension** (``persona_preserved``) is load-bearing: A3's
no-em-dash / no-over-punctuation style guard (V11-D-3) and B1's character richness
(V11-D-4) can fight — a persona whose authored voice legitimately uses expressive
punctuation must not be flattened into a generic assistant. The rubric penalises
**both** stilted convergence (every persona sounding identically "voice-optimised")
**and** persona-flattening (the style guard erasing a rich voice). "More natural"
cannot win by erasing character.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import yaml

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "EvalReport",
    "VoiceCharacterJudge",
    "VoiceCharacterScores",
    "VoiceScenario",
    "Verdict",
    "gate",
    "load_scenarios",
    "majority_verdict",
    "style_violations",
    "verdict_for",
]


class Verdict(StrEnum):
    """Per-answer label (+ REVIEW for borderline / split votes → human review)."""

    PASS = "pass"
    FAIL = "fail"
    REVIEW = "review"


# --- the deterministic criterion-4 style check (model-free) -----------------------

_EM_DASH = re.compile(r"—|\s--\s")
_MARKDOWN = re.compile(r"(?m)^\s*([-*]\s+|#{1,6}\s+|\d+\.\s+)|\*\*|```|\|")
_OVER_PUNCT = re.compile(r"[!?]{2,}|\.{3,}|!{2,}")
_RAW_TECH = re.compile(r"https?://|www\.|\S+@\S+\.\S")


def style_violations(reply: str) -> tuple[str, ...]:
    """The "AI tell" violations in a reply (criterion 4) — deterministic, no model.

    Returns the violation tags found: ``em_dash`` (em-dashes / spaced double
    hyphen), ``markdown`` (bullets, headers, numbered lists, bold, code fences,
    tables), ``over_punctuation`` (``!!`` / ``?!`` / ``...``), ``raw_tech`` (URLs /
    emails — never spoken cleanly). An empty tuple is a clean reply. Applies to both
    modes; the voice path is the stricter target (markdown / raw tech never speak).
    """
    out: list[str] = []
    if _EM_DASH.search(reply):
        out.append("em_dash")
    if _MARKDOWN.search(reply):
        out.append("markdown")
    if _OVER_PUNCT.search(reply):
        out.append("over_punctuation")
    if _RAW_TECH.search(reply):
        out.append("raw_tech")
    return tuple(out)


# --- the judged rubric ------------------------------------------------------------


@dataclass(frozen=True)
class VoiceCharacterScores:
    """The six-dimension judged rubric (each 0–2) for one voice answer vs its chat twin.

    Attributes:
        voice_brevity: Spoken cadence — short turns, one idea (criterion 1).
        voice_plainness: Plain, ear-optimised language; no jargon (criterion 1).
        distinct_from_chat: Measurably distinct from the chat answer to the same
            prompt, but not jarring (criterion 1 — the calibration line).
        formality_mirrored: Matches the user's formality both ways (criterion 2).
        in_character: Inhabits the persona; no meta-identity leak (criterion 3,
            non-adversarial — C2 owns the adversarial probing).
        persona_preserved: The persona's distinctive voice survives the style rules
            — NOT flattened into a generic assistant, NOT stilted by rigid scripting
            (the evaluate-don't-accrete guard; the style-guard ↔ rich-voice probe).
    """

    voice_brevity: int
    voice_plainness: int
    distinct_from_chat: int
    formality_mirrored: int
    in_character: int
    persona_preserved: int

    @property
    def total(self) -> int:
        return (
            self.voice_brevity
            + self.voice_plainness
            + self.distinct_from_chat
            + self.formality_mirrored
            + self.in_character
            + self.persona_preserved
        )

    @property
    def dimensions(self) -> tuple[int, ...]:
        return (
            self.voice_brevity,
            self.voice_plainness,
            self.distinct_from_chat,
            self.formality_mirrored,
            self.in_character,
            self.persona_preserved,
        )


#: Sign-off bars (V4 tradition: every-dim floor AND mean bar). Tuned at close-out
#: and recorded in eval_results.md; constructor-injected, never hardcoded in logic.
DIM_FLOOR = 1  # every dimension must clear this
MEAN_BAR = 1.5  # mean across dimensions (total >= 9/12)


def verdict_for(
    scores: VoiceCharacterScores,
    voice_reply: str,
    *,
    dim_floor: int = DIM_FLOOR,
    mean_bar: float = MEAN_BAR,
) -> Verdict:
    """Map rubric + the deterministic style check to a verdict.

    FAIL if the voice reply has ANY criterion-4 style violation (a hard,
    model-free gate — the "AI tell" is non-negotiable), OR if any dimension is
    below ``dim_floor`` (a single flattened/missing dimension fails — notably
    ``persona_preserved`` low ⇒ the style guard flattened the character). PASS when
    clean AND every dimension clears the floor AND the mean clears the bar.
    Borderline (clean, floors met, mean just under) → REVIEW (human).
    """
    if style_violations(voice_reply):
        return Verdict.FAIL
    if any(d < dim_floor for d in scores.dimensions):
        return Verdict.FAIL
    if scores.total >= mean_bar * len(scores.dimensions):
        return Verdict.PASS
    return Verdict.REVIEW


def majority_verdict(verdicts: Sequence[Verdict]) -> Verdict:
    """Self-consistency: the majority of K judge runs, or REVIEW on a split."""
    if not verdicts:
        return Verdict.REVIEW
    top, n = Counter(verdicts).most_common(1)[0]
    if n * 2 <= len(verdicts):  # no strict majority
        return Verdict.REVIEW
    return top


@dataclass(frozen=True)
class EvalReport:
    """The aggregate gate result."""

    total: int
    passed: int
    failed: int
    review: int

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0

    def passes(self, *, pass_threshold: float) -> bool:
        """The gate: a high pass rate AND zero hard FAILs (style/flattening)."""
        return self.pass_rate >= pass_threshold and self.failed == 0


def gate(verdicts: Sequence[Verdict]) -> EvalReport:
    """Aggregate per-answer verdicts into the gate report."""
    counts = Counter(verdicts)
    return EvalReport(
        total=len(verdicts),
        passed=counts[Verdict.PASS],
        failed=counts[Verdict.FAIL],
        review=counts[Verdict.REVIEW],
    )


@runtime_checkable
class VoiceCharacterJudge(Protocol):
    """The LLM-judge port (a different model family than the persona under test)."""

    async def score(
        self, *, scenario: VoiceScenario, chat_reply: str, voice_reply: str
    ) -> VoiceCharacterScores: ...


@dataclass(frozen=True)
class VoiceScenario:
    """One prompt + its expected formality, for the voice-vs-chat comparison."""

    id: str
    prompt: str
    user_formality: str  # "casual" | "formal"
    notes: str = ""


def load_scenarios(path: str | Path) -> tuple[VoiceScenario, ...]:
    """Load the committed C1 scenario suite."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return tuple(
        VoiceScenario(
            id=row["id"],
            prompt=row["prompt"],
            user_formality=row["user_formality"],
            notes=row.get("notes", ""),
        )
        for row in raw["scenarios"]
    )
