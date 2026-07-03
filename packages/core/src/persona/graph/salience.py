"""Evidence-salience — pure event functions, NEVER wall time (Spec K7, T6 / K7-D-6).

Salience is a *ranking / lifecycle* signal that moves by **evidence**, not by the
clock: corroboration strengthens, contradiction/disuse weaken, recall reinforces. A
two-year-old identity fact with no contradicting evidence keeps its salience —
uniform time decay measured ~18× harmful (arXiv 2604.26970), whose own recommended
drivers (velocity, volatility) are evidence-stream properties, not timestamps.

Every function here is a **pure function of the ordered evidence-event log**: it takes
the current salience + an ordinal *epoch* (the per-owner consolidation-run counter, an
evidence clock — never ``now()``) and returns the new salience, clamped to
``[floor, cap]``. This is what makes salience **time-shift invariant** (the same event
sequence yields the same salience regardless of the wall-clock timestamps on the
events) and lets the no-wallclock structural test pass: no path here reads a clock.

Disuse is the one relative signal — a node idle for more than a per-kind grace of
*epochs* decays one per-kind step **per pass**; ``TRAIT`` never fades by disuse
(identity facts), ``CIRCUMSTANCE`` fastest (the 2604.26970 type hierarchy over our
kinds), and ``SELF`` is outside salience entirely (K7-D-7).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.graph.models import NodeKind

if TYPE_CHECKING:
    from persona.graph.config import GraphSettings

__all__ = [
    "SALIENCE_EXCLUDED_KINDS",
    "clamp",
    "contradict",
    "corroborate",
    "disuse_decay",
    "reinforce",
]

#: Kinds outside the salience system (K7-D-7): the SELF anchor never gains or loses
#: salience — it is not an accumulating fact, it is the identity anchor.
SALIENCE_EXCLUDED_KINDS: frozenset[NodeKind] = frozenset({NodeKind.SELF})


def clamp(value: float, settings: GraphSettings) -> float:
    """Clamp a salience value to the configured ``[floor, cap]`` band (K7-D-6)."""
    return max(settings.salience_floor, min(settings.salience_cap, value))


def corroborate(salience: float, settings: GraphSettings) -> float:
    """A corroborating event (extend / re-merge-with-new-evidence / absorption) → ``+δ_c``."""
    return clamp(salience + settings.salience_delta_corroboration, settings)


def contradict(salience: float, settings: GraphSettings) -> float:
    """A contradicting event (a superseding evolve — the volatility signal) → ``−δ_x``."""
    return clamp(salience - settings.salience_delta_contradiction, settings)


def reinforce(salience: float, settings: GraphSettings) -> float:
    """A recall-reinforcement event (the node entered a prompt) → ``+δ_r`` (off the hot path)."""
    return clamp(salience + settings.salience_delta_recall, settings)


def disuse_decay(
    salience: float,
    node_kind: NodeKind,
    *,
    current_epoch: int,
    last_evidence_epoch: int,
    settings: GraphSettings,
) -> float:
    """Decay a node idle beyond its per-kind grace by ONE per-kind step (K7-D-6).

    Idleness is measured in ordinal evidence epochs (``current_epoch −
    last_evidence_epoch``), never wall time. Applied once per consolidation pass; over
    successive idle passes the per-step decays accumulate. ``TRAIT`` (``δ_d = 0``) and
    ``SELF`` never fade by disuse; a node still inside its grace is unchanged.
    """
    if node_kind in SALIENCE_EXCLUDED_KINDS:
        return salience
    idle = current_epoch - last_evidence_epoch
    grace = settings.disuse_grace_for(str(node_kind))
    if idle <= grace:
        return salience
    return clamp(salience - settings.disuse_delta_for(str(node_kind)), settings)
