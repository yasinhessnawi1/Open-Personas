"""Event-trigger recognition contracts — the A7 half of standing-intent recognition (Spec A7, T8).

The event analogue of the models :mod:`recognizer` owns for the schedule path. Kept in its own
module so the A4 recogniser stays untouched and :mod:`event_judge` can import the verdict/judgment
without a cycle. :class:`~recognizer.StandingIntentRecognizer` composes an injected
:class:`EventTriggerIntentJudge` (when A7 is enabled) to turn an event-conditioned turn into a
STANDING :class:`~persona_runtime.task_origination.draft.ContractDraft` carrying a
:class:`~persona.events.TriggerSpec` — which then flows through the EXISTING echo → confirm →
OriginationService door (no second creation path).
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, model_validator

from persona_runtime.questions import ProactiveQuestion, QuestionOption
from persona_runtime.task_origination.draft import (  # noqa: TC001 — Pydantic needs runtime access
    ContractDraft,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable

__all__ = [
    "EventTriggerIntentJudge",
    "EventTriggerJudgment",
    "EventTriggerVerdict",
    "build_event_clarify_question",
]


class EventTriggerVerdict(StrEnum):
    """The judge's event-vs-not judgment (step 2)."""

    TRIGGER = "trigger"
    NOT_TRIGGER = "not_trigger"
    AMBIGUOUS = "ambiguous"


class EventTriggerJudgment(BaseModel):
    """The judge's verdict, with a trigger-bearing draft iff the verdict is ``TRIGGER``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: EventTriggerVerdict
    draft: ContractDraft | None = None

    @model_validator(mode="after")
    def _draft_iff_trigger(self) -> EventTriggerJudgment:
        if self.verdict is EventTriggerVerdict.TRIGGER and self.draft is None:
            msg = "a TRIGGER judgment must carry a draft"
            raise ValueError(msg)
        if self.verdict is not EventTriggerVerdict.TRIGGER and self.draft is not None:
            msg = "only a TRIGGER judgment may carry a draft"
            raise ValueError(msg)
        if self.draft is not None and self.draft.trigger is None:
            msg = "a TRIGGER judgment's draft must carry a trigger (schedule XOR trigger, A7-D-3)"
            raise ValueError(msg)
        return self


@runtime_checkable
class EventTriggerIntentJudge(Protocol):
    """The model-backed step-2 decision for event intent (injected; the precision layer).

    Decides message-watch-vs-not in context and, for a clear watch, drafts the typed filter. It is
    the *only* place the decision is made — the cue net never decides — and it never guesses a
    filter (a wrong watched-sender is a privacy-adjacent miss; unsure ⇒ AMBIGUOUS, we ask).
    """

    def judge(self, message: str, *, language: str) -> Awaitable[EventTriggerJudgment]:
        """Judge whether ``message`` carries a standing message-watch; draft it when it does."""
        ...


# Per-language clarify ("I can watch for that — which sender or what should I look for?"). EN is the
# fallback. The 3+1 shape reuses the Spec-21 rail so the reply rides the existing answer path.
_CLARIFY_TEXT: dict[str, str] = {
    "en": "I can watch for that. Which sender or what exactly should I look for?",
    "nb": "Det kan jeg følge med på. Hvilken avsender eller hva nøyaktig skal jeg se etter?",
    "no": "Det kan jeg følge med på. Hvilken avsender eller hva nøyaktig skal jeg se etter?",
    "ar": "يمكنني مراقبة ذلك. أي مُرسِل أو ما الذي يجب أن أبحث عنه بالضبط؟",
}
_CLARIFY_OPTIONS: dict[str, tuple[tuple[str, str], tuple[str, str], tuple[str, str]]] = {
    "en": (
        ("Name the sender", "Tell me the exact email/number/handle to watch."),
        ("Watch for a keyword", "Watch for messages containing a specific word instead."),
        ("Never mind", "Skip setting up a watch."),
    ),
    "nb": (
        ("Oppgi avsender", "Si hvilken e-post/nummer/konto jeg skal følge med på."),
        ("Følg et nøkkelord", "Følg med på meldinger som inneholder et bestemt ord."),
        ("Glem det", "Hopp over."),
    ),
    "ar": (
        ("حدّد المُرسِل", "أخبرني بالبريد/الرقم/الحساب المحدد للمراقبة."),
        ("راقب كلمة مفتاحية", "راقب الرسائل التي تحتوي على كلمة محددة."),
        ("لا داعي", "تخطَّ ذلك."),
    ),
}


def build_event_clarify_question(language: str) -> ProactiveQuestion:
    """Build the single "which sender/what?" clarify question in the persona's language.

    Falls back to English for any language without a localised form. Conservative by design: when
    the watch subject is unpinnable, we ask for it — never invent one (criterion 3).
    """
    key = language if language in _CLARIFY_TEXT else "en"
    options_key = key if key in _CLARIFY_OPTIONS else "en"
    options = tuple(
        QuestionOption(label=label, description=description)
        for label, description in _CLARIFY_OPTIONS[options_key]
    )
    return ProactiveQuestion(question=_CLARIFY_TEXT[key], options=options, allow_free_form=True)
