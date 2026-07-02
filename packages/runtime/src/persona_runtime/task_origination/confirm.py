"""Interpreting the confirm reply to a contract proposal (Spec A4, T6 loop wiring).

The load-bearing safety property is "no standing/scheduled/spending task without **exactly one
explicit confirmation**". So :func:`is_affirmative_confirmation` is **precision-biased**: only a
reply that is *entirely* a clear affirmative confirms. "yes" / "go ahead" / "ja, gjør det"
confirm; "yes but make it 8am" does **not** (it carries a modification — an adjustment, not a
clean yes) and so cannot accidentally create a task. Anything that is not a clean confirmation
falls through to ordinary handling (the proposal is simply not acted on).
"""

from __future__ import annotations

import re

__all__ = ["is_affirmative_confirmation"]

# Anchored, full-string affirmatives (NO/AR/EN). The whole reply must be the affirmation (plus
# trailing punctuation) — a trailing clause means the user is adjusting, not confirming.
_AFFIRM_RE = re.compile(
    r"^\s*(?:"
    r"yes|yep|yeah|yes\s+please|please\s+do|confirm|confirmed|go\s+ahead|do\s+it|"
    r"set\s+it\s+up|sounds\s+good|"  # English
    r"ja|ja\s+takk|gjør\s+det|sett\s+i\s+gang|bekreft|"  # Norwegian
    r"نعم|أكد|تمام|افعلها|نفذها"  # Arabic
    r")\s*[!.…]*\s*$",
    re.IGNORECASE | re.UNICODE,
)


def is_affirmative_confirmation(reply: str) -> bool:
    """Whether ``reply`` is a clean, whole-message confirmation (precision-biased).

    Returns ``True`` only when the entire reply is a clear affirmative; a confirmation carrying
    any further instruction ("yes, but…") returns ``False`` so it cannot create a task by
    accident — the safe direction for a path that schedules and spends.
    """
    return bool(_AFFIRM_RE.match(reply))
