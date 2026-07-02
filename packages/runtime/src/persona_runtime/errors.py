"""Runtime domain exceptions (D-05-2).

The runtime is a composition layer (hexagonal architecture,
ENGINEERING_STANDARDS.md §1.2). It defines exactly **one** new exception —
:class:`TierNotConfiguredError` — for the one genuinely-new failure mode: a
model tier that cannot be resolved even after fallback. Every other failure
(provider 429s, tool-not-allowed, schema mismatches) is a spec-01/02/03 domain
exception that the runtime lets propagate **unchanged**, rather than wrapping it
in a parallel runtime vocabulary the caller would then have to unwrap.

Note on ``MaxToolRoundsExceeded``: there is deliberately **no** such exception.
Hitting ``max_tool_rounds`` is not an error — the loop handles it gracefully
(spec §4.2: append a system nudge, do one final generation). Adding an exception
here would invert that contract; don't.
"""

from __future__ import annotations

from persona.errors import PersonaError

__all__ = [
    "InvalidQuestionAnswerError",
    "ScheduleParseError",
    "TierNotConfiguredError",
]


class TierNotConfiguredError(PersonaError):
    """No model tier could be resolved for a requested tier name.

    Raised by :class:`persona_runtime.tier.TierRegistry` when a tier name does
    not resolve even after the ``small → mid → frontier`` fallback and the
    single-backend fallback (D-05-3). Carries ``context`` with the requested
    tier and the configured tier names so the operator can see the gap.
    """


class ScheduleParseError(PersonaError):
    """A schedule phrase could not be faithfully represented as an A1 cadence (Spec A4).

    Raised by :mod:`persona_runtime.task_origination.schedule` when a candidate cannot
    form a valid :class:`persona.schedules.RecurrenceRule` / one-time instant, or names an
    unknown timezone. This is the **parse-honesty** boundary (criterion 4): the persona
    declines plainly and offers an alternative rather than silently approximating a fuzzy
    intent. ``context`` carries the offending phrase and, where known, a suggested
    ``alternative`` the decline message can offer.
    """


class InvalidQuestionAnswerError(PersonaError):
    """An answer to a proactive question matches neither an option nor free-form.

    Spec 21 (D-21-9). Raised by :func:`persona_runtime.questions.validate_answer`
    when a submitted answer is not one of the question's predefined option
    labels and is not an acceptable free-form submission (empty, or free-form
    disallowed). This is the server-side validation that makes the structured
    3+1 options meaningful rather than decorative — the boundary rejects junk
    answers (fail-fast) instead of forwarding them into the loop. ``context``
    carries the offending answer and the legal option labels.
    """
