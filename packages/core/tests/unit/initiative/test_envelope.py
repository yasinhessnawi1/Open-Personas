"""Unit tests — the envelope decision (Spec A5, T2; spec §2 act-within/propose-at-gates)."""

from __future__ import annotations

import itertools

import pytest
from persona.initiative import InitiativeDial
from persona.initiative.envelope import EnvelopeAction, autonomy_rank, decide_envelope
from persona.tools.categories import FREE_CATEGORIES, GATED_BY_DEFAULT, ActionCategory

_ACT_DIAL = InitiativeDial.ACT_WITHIN_ENVELOPE

# A representative sweep of footprints: every all-safe subset shape, every gated
# category alone, mixed free+gated, and the empty set (the borderline).
_ALL_SAFE_FOOTPRINTS = [
    frozenset({ActionCategory.OBSERVE}),
    frozenset({ActionCategory.DRAFT}),
    frozenset({ActionCategory.OBSERVE, ActionCategory.COMPUTE}),
    FREE_CATEGORIES,
]
_GATED_FOOTPRINTS = [frozenset({gated}) for gated in GATED_BY_DEFAULT] + [
    frozenset({ActionCategory.OBSERVE, ActionCategory.SPEND}),  # mixed: any-gated wins
    FREE_CATEGORIES | frozenset({ActionCategory.EXTERNAL_MUTATE}),
]


class TestActVsPropose:
    @pytest.mark.parametrize("footprint", _ALL_SAFE_FOOTPRINTS)
    def test_all_safe_acts_under_the_earned_dial(
        self, footprint: frozenset[ActionCategory]
    ) -> None:
        assert decide_envelope(footprint, _ACT_DIAL) is EnvelopeAction.ACT

    @pytest.mark.parametrize("footprint", _GATED_FOOTPRINTS)
    def test_any_gated_proposes_even_under_the_earned_dial(
        self, footprint: frozenset[ActionCategory]
    ) -> None:
        assert decide_envelope(footprint, _ACT_DIAL) is EnvelopeAction.PROPOSE

    def test_borderline_empty_footprint_proposes(self) -> None:
        """Not provably safe ⇒ propose — the bias is the default branch, proven."""
        assert decide_envelope(frozenset(), _ACT_DIAL) is EnvelopeAction.PROPOSE

    def test_act_requires_proof_of_safety_not_absence_of_gates(self) -> None:
        """The decision is `footprint ⊆ FREE`, never `footprint ∩ GATED == ∅` alone.

        The two differ exactly on the empty/unknown footprint — the borderline —
        and the implementation must sit on the propose side of it.
        """
        empty: frozenset[ActionCategory] = frozenset()
        assert empty & GATED_BY_DEFAULT == frozenset()  # no gate present...
        assert decide_envelope(empty, _ACT_DIAL) is EnvelopeAction.PROPOSE  # ...still proposes


class TestDialMonotonicity:
    def test_off_yields_none_for_every_footprint(self) -> None:
        for footprint in [*_ALL_SAFE_FOOTPRINTS, *_GATED_FOOTPRINTS, frozenset()]:
            assert decide_envelope(footprint, InitiativeDial.OFF) is EnvelopeAction.NONE

    def test_propose_only_converts_would_be_acts(self) -> None:
        for footprint in _ALL_SAFE_FOOTPRINTS:
            assert decide_envelope(footprint, InitiativeDial.PROPOSE_ONLY) is EnvelopeAction.PROPOSE

    def test_monotone_conservative_ordering_property(self) -> None:
        """For EVERY footprint, a lower dial never yields a more autonomous action.

        OFF < PROPOSE_ONLY < ACT_WITHIN_ENVELOPE must map to autonomy ranks that
        are non-decreasing — the dial can only unlock autonomy, never have a
        lower setting act where a higher one proposes (T2 bar 4).
        """
        dial_order = [InitiativeDial.OFF, InitiativeDial.PROPOSE_ONLY, _ACT_DIAL]
        footprints = [*_ALL_SAFE_FOOTPRINTS, *_GATED_FOOTPRINTS, frozenset()]
        for footprint, (lower, higher) in itertools.product(
            footprints, itertools.combinations(dial_order, 2)
        ):
            assert autonomy_rank(decide_envelope(footprint, lower)) <= autonomy_rank(
                decide_envelope(footprint, higher)
            )
