"""Task auto-dispatch orchestration — the API-layer trigger (spec 21 T10).

D-21-10 layer split: detection + scope-check are pure and live in
``persona-runtime`` (``task_detector``); the *dispatch trigger* — consult the
consent gate, create the run — lives here in the API layer (Run creation is an
API concern). A false-positive detection costs at most a consent prompt, never
an unwanted run.

The decision core (:func:`decide`) is a pure truth table over the detection and
the persona's tri-state consent; :func:`auto_dispatch` adds the side effects
(read consent, start the run). The consent question reuses the spec-21
:class:`~persona_runtime.questions.ProactiveQuestion` 3+1 vocabulary
(D-21-16 single answer surface); the answer arrives as the next chat turn and is
mapped back by :func:`parse_consent_answer`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from persona_runtime.questions import ProactiveQuestion, QuestionOption
from persona_runtime.task_detector import TaskDetection, default_registry

from persona_api.services import consent_service

if TYPE_CHECKING:
    from persona.schema.persona import Persona
    from sqlalchemy.engine import Engine

    from persona_api.jobs.queue import JobQueue
    from persona_api.services.consent_service import ConsentState


__all__ = [
    "DispatchOutcome",
    "DispatchResult",
    "auto_dispatch",
    "consent_question",
    "decide",
    "detect_task",
    "parse_consent_answer",
]

#: ``none`` — not a task (normal chat); ``clarify`` — ambiguous task (ask which);
#: ``dispatch`` — granted, create the run; ``ask_consent`` — first task, prompt;
#: ``declined`` — consent declined, normal chat.
DispatchOutcome = Literal["none", "clarify", "dispatch", "ask_consent", "declined"]

# The first-task consent question's three options (D-21-16). Labels are the
# answers the orchestrator maps via parse_consent_answer.
_CONSENT_GRANT = "Yes, run tasks automatically"
_CONSENT_DECLINE = "No, don't run this"
_CONSENT_MODIFY = "Let me adjust it first"


@dataclass(frozen=True)
class DispatchResult:
    """The orchestrator's verdict for one chat message (T10)."""

    outcome: DispatchOutcome
    detection: TaskDetection | None = None
    task_id: str | None = None  # Spec W1: the ad hoc task, not a bare run
    question: ProactiveQuestion | None = None


def detect_task(persona: Persona, message: str) -> TaskDetection | None:
    """Detect a task mapping to one of ``persona``'s declared capabilities (T08 bridge)."""
    return default_registry(persona).detect(message)


def decide(detection: TaskDetection | None, consent: ConsentState) -> DispatchOutcome:
    """Pure decision: what to do given a detection and the persona's consent state.

    Truth table (D-21-7/13/16/17):
    - no detection → ``none`` (normal chat)
    - detection but ambiguous → ``clarify`` (ask which capability)
    - dispatchable + granted (``True``) → ``dispatch``
    - dispatchable + never-asked (``None``) → ``ask_consent``
    - dispatchable + declined (``False``) → ``declined`` (normal chat, no re-prompt)
    """
    if detection is None:
        return "none"
    if not detection.dispatchable:
        return "clarify"
    if consent is True:
        return "dispatch"
    if consent is None:
        return "ask_consent"
    return "declined"


def consent_question(task_summary: str) -> ProactiveQuestion:
    """Build the first-task consent question in the 3+1 format (D-21-16)."""
    return ProactiveQuestion(
        question=(
            f'I can handle that for you: "{task_summary}". Allow this persona to start '
            "tasks like this automatically? You can turn this off anytime in settings, "
            "and every task shows up in the activity log."
        ),
        options=(
            QuestionOption(
                label=_CONSENT_GRANT, description="Run this and future tasks without asking"
            ),
            QuestionOption(label=_CONSENT_DECLINE, description="Don't run it; keep chatting"),
            QuestionOption(label=_CONSENT_MODIFY, description="Let me refine the request first"),
        ),
    )


def parse_consent_answer(answer: str) -> Literal["grant", "decline", "modify"]:
    """Map a consent answer (option label or free-form) to an action (D-21-16).

    Defaults to ``modify`` on an unrecognised free-form answer — never grants on
    ambiguity (consent must be a clear affirmative).
    """
    text = answer.strip().casefold()
    if text == _CONSENT_GRANT.casefold() or text in {"yes", "y", "ok", "sure"}:
        return "grant"
    if text == _CONSENT_DECLINE.casefold() or text in {"no", "n", "nope"}:
        return "decline"
    return "modify"


async def auto_dispatch(
    *,
    rls_engine: Engine,
    queue: JobQueue,
    owner_id: str,
    persona_id: str,
    persona: Persona,
    message: str,
) -> DispatchResult:
    """Detect → consult consent → maybe create a run (the T10 trigger).

    Returns a :class:`DispatchResult` the route acts on: ``dispatch`` carries the
    new ``run_id``; ``ask_consent`` carries the consent question; the rest fall
    through to normal chat. Consent is re-read here on every call (D-21-7) —
    never cached.
    """
    detection = detect_task(persona, message)
    if detection is None:
        return DispatchResult("none")
    if not detection.dispatchable:
        return DispatchResult("clarify", detection=detection)

    consent = consent_service.read_consent(rls_engine=rls_engine, persona_id=persona_id)
    outcome = decide(detection, consent)
    if outcome == "dispatch":
        # Spec W1 (D-W1-1): a chat-detected one-off is an ad hoc TASK now, not a bare run.
        # Imported here: the dispatch service reaches the task handler, which reaches this
        # package's ``user_facing_errors`` through ``persona_api.services`` (a cycle at
        # import time otherwise; the run_service ``PLC0415`` precedent).
        from persona_api.services import work_dispatch_service  # noqa: PLC0415

        dispatched = work_dispatch_service.dispatch_ad_hoc(
            rls_engine=rls_engine,
            queue=queue,
            owner_id=owner_id,
            persona_id=persona_id,
            brief=message,
        )
        return DispatchResult("dispatch", detection=detection, task_id=dispatched.task_id)
    if outcome == "ask_consent":
        return DispatchResult(
            "ask_consent", detection=detection, question=consent_question(message)
        )
    return DispatchResult("declined", detection=detection)
