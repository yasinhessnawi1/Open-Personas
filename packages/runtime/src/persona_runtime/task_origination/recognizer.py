"""Standing-intent recognition — the two-step orchestration (Spec A4, T4; A4-D-2).

Step 1 (cheap, high-recall, non-deciding) is :func:`detect_standing_cue`; step 2 (the
decision) is the model-backed :class:`StandingIntentJudge`. :class:`StandingIntentRecognizer`
composes them:

- no cue → ``ORDINARY`` (the cheap path — no model call, ordinary chat);
- cue + judge says now-work → ``ORDINARY`` (the model rejected the false-admit);
- cue + judge says standing → ``STANDING`` carrying a canonicalised :class:`ContractDraft`;
- cue + judge says ambiguous, **or the judge fails** → ``ASK_ONCE`` ("on a schedule, or just
  this once?").

The safe bias is total: the recogniser **never** opens a task on its own — ``STANDING`` only
*proposes* a draft to echo, and creation still requires the explicit confirmation (T6). When
the model is unsure or unavailable, the outcome is to *ask*, never to assume — a recognition
failure degrades to a question, never to a silent drop (which would lose the standing intent)
and never to an unconfirmed task. The proposed draft is canonicalised so the downstream
idempotency token (T6) is stable.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from persona.logging import get_logger
from pydantic import BaseModel, ConfigDict, model_validator

from persona_runtime.questions import ProactiveQuestion, QuestionOption
from persona_runtime.task_origination.cues import detect_standing_cue
from persona_runtime.task_origination.draft import ContractDraft, canonicalize_draft

if TYPE_CHECKING:
    from collections.abc import Awaitable

__all__ = [
    "RecognitionKind",
    "RecognitionOutcome",
    "StandingIntentJudge",
    "StandingIntentRecognizer",
    "StandingJudgment",
    "StandingVerdict",
    "build_clarify_question",
]

_logger = get_logger("runtime.task_origination")


class StandingVerdict(StrEnum):
    """The model's standing-vs-now judgment (step 2)."""

    NOW_WORK = "now_work"
    STANDING = "standing"
    AMBIGUOUS = "ambiguous"


class StandingJudgment(BaseModel):
    """The judge's verdict, with a drafted contract iff the verdict is ``STANDING``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: StandingVerdict
    draft: ContractDraft | None = None

    @model_validator(mode="after")
    def _draft_iff_standing(self) -> StandingJudgment:
        if self.verdict is StandingVerdict.STANDING and self.draft is None:
            msg = "a STANDING judgment must carry a draft"
            raise ValueError(msg)
        if self.verdict is not StandingVerdict.STANDING and self.draft is not None:
            msg = "only a STANDING judgment may carry a draft"
            raise ValueError(msg)
        return self


@runtime_checkable
class StandingIntentJudge(Protocol):
    """The model-backed step-2 decision (injected; the precision layer).

    Implementations decide standing-vs-now in context and, for standing intent, draft the
    contract fields. They are the *only* place the standing-vs-now decision is made — the cue
    net never decides. A judge should never *create* anything; it only judges and drafts.
    """

    def judge(self, message: str, *, language: str) -> Awaitable[StandingJudgment]:
        """Judge whether ``message`` carries standing intent; draft it when it does."""
        ...


class RecognitionKind(StrEnum):
    """What the recogniser concluded for this turn."""

    ORDINARY = "ordinary"
    STANDING = "standing"
    ASK_ONCE = "ask_once"


class RecognitionOutcome(BaseModel):
    """The recogniser's conclusion: ordinary chat, a standing draft, or a clarifying ask."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: RecognitionKind
    draft: ContractDraft | None = None
    question: ProactiveQuestion | None = None

    @model_validator(mode="after")
    def _payload_matches_kind(self) -> RecognitionOutcome:
        if self.kind is RecognitionKind.STANDING and self.draft is None:
            msg = "a STANDING outcome must carry a draft"
            raise ValueError(msg)
        if self.kind is RecognitionKind.ASK_ONCE and self.question is None:
            msg = "an ASK_ONCE outcome must carry a question"
            raise ValueError(msg)
        if self.kind is RecognitionKind.ORDINARY and (self.draft or self.question):
            msg = "an ORDINARY outcome carries neither a draft nor a question"
            raise ValueError(msg)
        return self

    @classmethod
    def ordinary(cls) -> RecognitionOutcome:
        """No contract path — proceed as ordinary chat."""
        return cls(kind=RecognitionKind.ORDINARY)

    @classmethod
    def standing(cls, draft: ContractDraft) -> RecognitionOutcome:
        """Propose a standing contract draft (to be echoed and confirmed)."""
        return cls(kind=RecognitionKind.STANDING, draft=draft)

    @classmethod
    def ask_once(cls, question: ProactiveQuestion) -> RecognitionOutcome:
        """Ask the one clarifying question (schedule-or-once)."""
        return cls(kind=RecognitionKind.ASK_ONCE, question=question)


# Per-language clarifying question ("on a schedule, or just this once?"). EN is the fallback.
_CLARIFY_TEXT: dict[str, str] = {
    "en": "Want me to keep doing this on a schedule, or just this once?",
    "nb": "Vil du at jeg skal gjøre dette fast etter en plan, eller bare denne ene gangen?",
    "no": "Vil du at jeg skal gjøre dette fast etter en plan, eller bare denne ene gangen?",
    "ar": "هل تريد أن أقوم بهذا بشكل دوري وفق جدول، أم مرة واحدة فقط؟",
}
_CLARIFY_OPTIONS: dict[str, tuple[tuple[str, str], tuple[str, str], tuple[str, str]]] = {
    "en": (
        ("Keep it on a schedule", "I'll set it up as a standing task."),
        ("Just this once", "I'll handle it now and not repeat it."),
        ("Never mind", "Skip it."),
    ),
    "nb": (
        ("Gjør det fast", "Jeg setter det opp som en fast oppgave."),
        ("Bare denne gangen", "Jeg gjør det nå og gjentar det ikke."),
        ("Glem det", "Hopp over."),
    ),
    "ar": (
        ("اجعلها دورية", "سأعدّها كمهمة دائمة."),
        ("مرة واحدة فقط", "سأقوم بها الآن دون تكرار."),
        ("لا داعي", "تخطَّ ذلك."),
    ),
}


def build_clarify_question(language: str) -> ProactiveQuestion:
    """Build the single schedule-or-once clarifying question in the persona's language.

    Falls back to English for any language without a localised form. The 3+1 shape reuses the
    Spec-21 :class:`ProactiveQuestion` rail, so the user's reply rides the existing
    next-turn answer path.
    """
    key = language if language in _CLARIFY_TEXT else "en"
    options_key = key if key in _CLARIFY_OPTIONS else "en"
    options = tuple(
        QuestionOption(label=label, description=description)
        for label, description in _CLARIFY_OPTIONS[options_key]
    )
    return ProactiveQuestion(question=_CLARIFY_TEXT[key], options=options, allow_free_form=True)


class StandingIntentRecognizer:
    """Compose the cue net (step 1) and the model judge (step 2) into one decision (A4-D-2)."""

    def __init__(self, judge: StandingIntentJudge) -> None:
        """Inject the model-backed judge (the precision layer)."""
        self._judge = judge

    async def recognize(self, message: str, *, language: str) -> RecognitionOutcome:
        """Recognise standing intent in ``message`` (cheap-trigger → model-decision).

        Returns ``ORDINARY`` with no model call when no cue fires; otherwise consults the
        judge. A judge failure degrades to ``ASK_ONCE`` (never a silent drop, never an
        unconfirmed task). A ``STANDING`` outcome carries a canonicalised draft.
        """
        if detect_standing_cue(message) is None:
            return RecognitionOutcome.ordinary()
        try:
            judgment = await self._judge.judge(message, language=language)
        except Exception:  # noqa: BLE001 — a judge failure must degrade to ask-once, never crash or auto-create
            _logger.warning("standing-intent judge failed; degrading to ask-once")
            return RecognitionOutcome.ask_once(build_clarify_question(language))
        if judgment.verdict is StandingVerdict.STANDING:
            assert judgment.draft is not None  # guaranteed by StandingJudgment validator
            return RecognitionOutcome.standing(canonicalize_draft(judgment.draft))
        if judgment.verdict is StandingVerdict.NOW_WORK:
            return RecognitionOutcome.ordinary()
        return RecognitionOutcome.ask_once(build_clarify_question(language))
