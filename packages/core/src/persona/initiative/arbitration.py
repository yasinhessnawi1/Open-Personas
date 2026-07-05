"""Duplicate-suppression arbitration — which persona voices an opportunity (A5-D-6).

The shared graph means every persona can see the same deadline; only one should
mention it. Arbitration is deterministic, explainable, and computed BEFORE the
ledger insert: (1) the persona whose conversations ground the candidate most —
the count of cited-node provenance contributions per persona; (2) tie → the
user's most-active persona; (3) still tied → lexicographic persona id (a total
order — no flake). The ledger's partial unique
(``UNIQUE(owner, opportunity_key) WHERE superseded_at IS NULL``) then makes any
remaining race harmless: the loser's insert no-ops, audited.

Pure — the pipeline supplies the provenance persona-ids (from the candidate's
resolved citations) and an activity ranking; no I/O here.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = ["arbitrate_voicer"]


def arbitrate_voicer(
    scanning_persona_id: str,
    *,
    citation_provenance_personas: Sequence[str],
    activity_rank: Mapping[str, int] | None = None,
) -> str:
    """The persona that voices this opportunity (A5-D-6's three-step total order).

    Args:
        scanning_persona_id: The persona whose scan produced the candidate (the
            fallback when no citation carries persona provenance).
        citation_provenance_personas: One entry per provenance contribution on
            the candidate's cited nodes that carries a ``persona_id`` (the
            pipeline flattens the trails; user/system contributions carry none
            and are simply absent here).
        activity_rank: Optional persona → recent-activity count (trailing
            window; higher = more active) for the tie-break.

    Returns:
        The voicing persona id. Deterministic for identical inputs.
    """
    if not citation_provenance_personas:
        return scanning_persona_id
    counts = Counter(citation_provenance_personas)
    best_grounding = max(counts.values())
    grounded_most = sorted(p for p, c in counts.items() if c == best_grounding)
    if len(grounded_most) == 1:
        return grounded_most[0]
    if activity_rank:
        best_activity = max(activity_rank.get(p, 0) for p in grounded_most)
        most_active = sorted(p for p in grounded_most if activity_rank.get(p, 0) == best_activity)
        return most_active[0]
    return grounded_most[0]
