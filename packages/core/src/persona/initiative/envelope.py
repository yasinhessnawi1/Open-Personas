"""The act/propose envelope decision (Spec A5, T2; spec §2, A5-D-5).

One rule, stated as the spec states it: **act within the envelope, propose at
the gates.** A candidate whose whole category footprint sits in A3's free set
may be done and presented done (act-then-report); the moment any step's
category is gated (spend, send, mutate, credentialed), initiative proposes
instead. The bias is structural: ``PROPOSE`` is the default branch — ``ACT``
is returned only when the footprint is *provably* all-safe AND the dial has
earned act-within-envelope. Borderline (anything not provably safe) resolves
to propose.

The dial composes monotone-conservatively: ``OFF`` yields nothing,
``PROPOSE_ONLY`` converts every would-be-act into a proposal, and only
``ACT_WITHIN_ENVELOPE`` unlocks acting — proven as an ordering property in the
T2 tests, not just implemented.

Pure — no I/O, no clock; the pipeline (T7) supplies the candidate footprint
and the persona's dial.
"""

from __future__ import annotations

from enum import StrEnum

from persona.initiative.dial import InitiativeDial
from persona.tools.categories import FREE_CATEGORIES, ActionCategory  # noqa: TC001 — runtime use

__all__ = ["EnvelopeAction", "decide_envelope"]


class EnvelopeAction(StrEnum):
    """What the envelope allows for a candidate (ordered by autonomy).

    ``NONE`` — the dial is off; nothing is raised. ``PROPOSE`` — surface as a
    proposal; the user's reply is the confirmation (A4's contract path or A8's
    reschedule door). ``ACT`` — execute as an implicit task and present the
    done work (act-then-report).
    """

    NONE = "none"
    PROPOSE = "propose"
    ACT = "act"


#: The autonomy ordering the dial must be monotone against (T2's ordering property).
_AUTONOMY_RANK: dict[EnvelopeAction, int] = {
    EnvelopeAction.NONE: 0,
    EnvelopeAction.PROPOSE: 1,
    EnvelopeAction.ACT: 2,
}


def autonomy_rank(action: EnvelopeAction) -> int:
    """The action's autonomy level (0 = silent, 2 = acts unprompted) — for ordering proofs."""
    return _AUTONOMY_RANK[action]


def decide_envelope(footprint: frozenset[ActionCategory], dial: InitiativeDial) -> EnvelopeAction:
    """Decide act vs propose from the plan's category footprint + the persona dial.

    The default branch is ``PROPOSE`` (the borderline-resolves-to-propose
    bias): ``ACT`` requires BOTH a non-empty footprint provably contained in
    :data:`~persona.tools.categories.FREE_CATEGORIES` AND
    ``InitiativeDial.ACT_WITHIN_ENVELOPE``. An empty footprint is not provably
    safe — a plan with no resolvable effect must not run unattended for free
    (the A3-D-X-completeness posture) — so it proposes.

    Args:
        footprint: The union of the candidate plan's step categories.
        dial: The persona's initiative dial (A5-D-5).

    Returns:
        ``NONE`` when the dial is off; otherwise ``ACT`` only for a provably
        all-safe footprint under the earned dial setting; otherwise ``PROPOSE``.
    """
    if dial is InitiativeDial.OFF:
        return EnvelopeAction.NONE
    all_safe = bool(footprint) and footprint <= FREE_CATEGORIES
    if all_safe and dial is InitiativeDial.ACT_WITHIN_ENVELOPE:
        return EnvelopeAction.ACT
    return EnvelopeAction.PROPOSE
