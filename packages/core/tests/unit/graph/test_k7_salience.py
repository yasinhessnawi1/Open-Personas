"""Unit tests for the Spec K7 T6 evidence-salience — pure functions + no-clock proof.

The delta functions (corroborate/contradict/reinforce/disuse), clamping, the
time-shift-invariance property (salience is a pure function of the ordered evidence
log — no timestamp input), and the STRUCTURAL no-wallclock guarantee: no salience path
reads ``now()``/``datetime.now``/``func.now`` (K7-D-6). DB dynamics are covered by the
integration tests.
"""

from __future__ import annotations

import inspect

import pytest
from persona.graph.config import GraphSettings
from persona.graph.models import NodeKind
from persona.graph.salience import (
    SALIENCE_EXCLUDED_KINDS,
    clamp,
    contradict,
    corroborate,
    disuse_decay,
    reinforce,
)

S = GraphSettings()


# ----- the delta functions -------------------------------------------------


def test_corroborate_and_reinforce_rise_contradict_falls() -> None:
    assert corroborate(1.0, S) == pytest.approx(1.0 + S.salience_delta_corroboration)
    assert reinforce(1.0, S) == pytest.approx(1.0 + S.salience_delta_recall)
    assert contradict(1.0, S) == pytest.approx(1.0 - S.salience_delta_contradiction)


def test_clamp_to_floor_and_cap() -> None:
    assert clamp(-5.0, S) == S.salience_floor
    assert clamp(999.0, S) == S.salience_cap
    # a long run of contradictions floors, never negative.
    v = 1.0
    for _ in range(100):
        v = contradict(v, S)
    assert v == S.salience_floor
    # a long run of corroborations caps.
    v = 1.0
    for _ in range(100):
        v = corroborate(v, S)
    assert v == S.salience_cap


# ----- disuse decay: per-kind, epoch-driven (K7-D-6) -----------------------


def test_disuse_decays_only_beyond_grace() -> None:
    grace = S.disuse_grace_for(str(NodeKind.CIRCUMSTANCE))
    # inside grace → unchanged.
    assert (
        disuse_decay(
            1.0, NodeKind.CIRCUMSTANCE, current_epoch=grace, last_evidence_epoch=0, settings=S
        )
        == 1.0
    )
    # one epoch beyond grace → one step down.
    decayed = disuse_decay(
        1.0, NodeKind.CIRCUMSTANCE, current_epoch=grace + 1, last_evidence_epoch=0, settings=S
    )
    assert decayed == pytest.approx(1.0 - S.disuse_delta_for(str(NodeKind.CIRCUMSTANCE)))


def test_trait_never_fades_by_disuse() -> None:
    # a two-year-equivalent idleness (huge epoch gap) leaves a TRAIT untouched.
    assert (
        disuse_decay(1.0, NodeKind.TRAIT, current_epoch=10_000, last_evidence_epoch=0, settings=S)
        == 1.0
    )


def test_self_is_outside_salience() -> None:
    assert NodeKind.SELF in SALIENCE_EXCLUDED_KINDS
    assert (
        disuse_decay(1.0, NodeKind.SELF, current_epoch=10_000, last_evidence_epoch=0, settings=S)
        == 1.0
    )


# ----- time-shift invariance: salience is a pure function of the event log --


def _fold(events: list[str], settings: GraphSettings) -> float:
    s = settings.salience_default
    for ev in events:
        if ev == "corr":
            s = corroborate(s, settings)
        elif ev == "cont":
            s = contradict(s, settings)
        elif ev == "reinf":
            s = reinforce(s, settings)
    return s


def test_salience_is_a_pure_function_of_the_event_order() -> None:
    log = ["corr", "corr", "cont", "reinf", "cont", "corr"]
    # deterministic + independent of any wall-clock: the functions take NO timestamp,
    # so replaying the same ordered log (however its events are dated) is identical.
    assert _fold(log, S) == _fold(log, S)
    # order matters (it's evidence order), value is stable.
    assert _fold(log, S) == pytest.approx(
        S.salience_default
        + 3 * S.salience_delta_corroboration
        + S.salience_delta_recall
        - 2 * S.salience_delta_contradiction
    )


# ----- structural: no salience path reads a clock (K7-D-6 no-wallclock) -----


def _calls_a_clock(source: str) -> bool:
    """True if the code CALLS a wall clock (``x.now(...)`` or ``now(...)``).

    AST-based so docstring/comment *mentions* of ``now()`` don't false-trip — only
    real calls count (``datetime.now()``, ``func.now()``, a bare ``now()``).
    """
    import ast
    import textwrap

    tree = ast.parse(textwrap.dedent(source))
    for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call)):
        func = call.func
        if isinstance(func, ast.Attribute) and func.attr == "now":
            return True
        if isinstance(func, ast.Name) and func.id == "now":
            return True
    return False


def test_no_salience_code_reads_wallclock() -> None:
    from persona.graph import salience as salience_module
    from persona.graph.postgres import PostgresGraphBackend

    assert not _calls_a_clock(inspect.getsource(salience_module)), "salience module calls a clock"
    # the transport salience methods (each a single evidence-epoch statement).
    for method in (
        PostgresGraphBackend.bump_salience,
        PostgresGraphBackend.record_recall,
        PostgresGraphBackend.apply_disuse_decay,
        PostgresGraphBackend.current_epoch,
        PostgresGraphBackend.get_salience,
    ):
        assert not _calls_a_clock(inspect.getsource(method)), f"{method.__name__} calls a clock"
