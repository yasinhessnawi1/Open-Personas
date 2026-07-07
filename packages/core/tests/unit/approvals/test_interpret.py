"""Tests for A3 reply interpretation — the security-sensitive classifier (A3-D-X-reply-parsing, T4).

The model is **never trusted**; the deterministic floor (:func:`resolve_reply`) is the
reliability bound. The properties under test are safety properties, not polish:

- **Approve only on a confident approve.** Ambiguous / low-confidence / unparseable / a reply
  that argues with the proposal → never an approval (clarify-once → then deny).
- **Deny is honoured at any confidence** (the user steering away is always safe to obey).
- **Clarify-once, then deny** — a gated action can't sit pending forever or be talked into
  approval over turns.
- **A modify routes through :func:`classify_modification`** — a material edit re-confirms; it
  never silently executes the edited action.
- **NO/EN both** — the conservative lexicon decides only bare unambiguous tokens and marks
  every hedged form ambiguous, so a mis-parsed ``nei`` / ``ja`` cannot become an approval.
"""

from __future__ import annotations

import pytest
from persona.approvals import DecisionType, Materiality
from persona.approvals.interpret import (
    InterpretedIntent,
    LexiconReplyInterpreter,
    RawInterpretation,
    ResolvedReply,
    resolve_reply,
)
from pydantic import ValidationError

_ORIGINAL = {"to": "bob@example.com", "amount": 1500, "body": "draft"}


def _raw(
    intent: InterpretedIntent, *, confidence: float = 0.95, edited: object = None
) -> RawInterpretation:
    return RawInterpretation(intent=intent, confidence=confidence, edited_arguments=edited)  # type: ignore[arg-type]


class TestFloorApprove:
    def test_confident_approve_approves(self) -> None:
        out = resolve_reply(
            _raw(InterpretedIntent.APPROVE), original_arguments=_ORIGINAL, clarifications_used=0
        )
        assert out.outcome is DecisionType.APPROVE

    def test_low_confidence_approve_never_approves(self) -> None:
        # The unforgivable-bug guard: a low-confidence approve clarifies, never approves.
        out = resolve_reply(
            _raw(InterpretedIntent.APPROVE, confidence=0.4),
            original_arguments=_ORIGINAL,
            clarifications_used=0,
        )
        assert out.outcome is DecisionType.CLARIFY
        # …and on the second pass it denies, never approves.
        out2 = resolve_reply(
            _raw(InterpretedIntent.APPROVE, confidence=0.4),
            original_arguments=_ORIGINAL,
            clarifications_used=1,
        )
        assert out2.outcome is DecisionType.DENY


class TestFloorDeny:
    def test_deny_is_honoured_at_any_confidence(self) -> None:
        for confidence in (0.95, 0.5, 0.1):
            out = resolve_reply(
                _raw(InterpretedIntent.DENY, confidence=confidence),
                original_arguments=_ORIGINAL,
                clarifications_used=0,
            )
            assert out.outcome is DecisionType.DENY


class TestFloorModify:
    def test_material_edit_modifies_and_flags_material(self) -> None:
        out = resolve_reply(
            _raw(InterpretedIntent.MODIFY, edited={**_ORIGINAL, "amount": 2000}),
            original_arguments=_ORIGINAL,
            clarifications_used=0,
        )
        assert out.outcome is DecisionType.MODIFY
        assert out.materiality is Materiality.MATERIAL
        assert out.edited_arguments == {**_ORIGINAL, "amount": 2000}

    def test_immaterial_edit_modifies_and_flags_immaterial(self) -> None:
        out = resolve_reply(
            _raw(InterpretedIntent.MODIFY, edited={**_ORIGINAL, "body": "new wording"}),
            original_arguments=_ORIGINAL,
            clarifications_used=0,
        )
        assert out.outcome is DecisionType.MODIFY
        assert out.materiality is Materiality.IMMATERIAL

    def test_modify_without_edits_is_treated_as_ambiguous(self) -> None:
        out = resolve_reply(
            _raw(InterpretedIntent.MODIFY, edited=None),
            original_arguments=_ORIGINAL,
            clarifications_used=0,
        )
        assert out.outcome is DecisionType.CLARIFY

    def test_low_confidence_modify_does_not_execute(self) -> None:
        out = resolve_reply(
            _raw(InterpretedIntent.MODIFY, confidence=0.4, edited={**_ORIGINAL, "amount": 2000}),
            original_arguments=_ORIGINAL,
            clarifications_used=0,
        )
        assert out.outcome is DecisionType.CLARIFY


class TestFloorClarifyOnceThenDeny:
    def test_ambiguous_clarifies_once_then_denies(self) -> None:
        first = resolve_reply(
            _raw(InterpretedIntent.AMBIGUOUS, confidence=0.2),
            original_arguments=_ORIGINAL,
            clarifications_used=0,
        )
        assert first.outcome is DecisionType.CLARIFY
        second = resolve_reply(
            _raw(InterpretedIntent.AMBIGUOUS, confidence=0.2),
            original_arguments=_ORIGINAL,
            clarifications_used=1,
        )
        assert second.outcome is DecisionType.DENY


class TestFloorNeverApprovesOnUncertainty:
    @pytest.mark.parametrize("clarifications_used", [0, 1, 2])
    def test_ambiguous_never_approves(self, clarifications_used: int) -> None:
        out = resolve_reply(
            _raw(InterpretedIntent.AMBIGUOUS, confidence=0.9),  # even a "confident" ambiguous
            original_arguments=_ORIGINAL,
            clarifications_used=clarifications_used,
        )
        assert out.outcome is not DecisionType.APPROVE


class TestResolvedReplyInvariants:
    def test_materiality_only_for_modify(self) -> None:
        with pytest.raises(ValidationError):
            ResolvedReply(outcome=DecisionType.APPROVE, materiality=Materiality.MATERIAL)
        with pytest.raises(ValidationError):
            ResolvedReply(outcome=DecisionType.MODIFY)  # modify must carry materiality + edits


class TestLexiconNorwegianEnglish:
    pytestmark = pytest.mark.asyncio

    @pytest.fixture
    def interp(self) -> LexiconReplyInterpreter:
        return LexiconReplyInterpreter()

    @pytest.mark.parametrize(
        "reply",
        ["yes", "Yes.", "ok", "approve", "send it", "ja", "ja!", "godkjenn", "greit"],
    )
    async def test_bare_affirmations_approve(
        self, interp: LexiconReplyInterpreter, reply: str
    ) -> None:
        raw = await interp.interpret(reply, proposal=None)  # type: ignore[arg-type]
        assert raw.intent is InterpretedIntent.APPROVE
        assert raw.confidence >= 0.75

    @pytest.mark.parametrize("reply", ["no", "No.", "nope", "deny", "nei", "avslå", "stopp"])
    async def test_bare_denials_deny(self, interp: LexiconReplyInterpreter, reply: str) -> None:
        raw = await interp.interpret(reply, proposal=None)  # type: ignore[arg-type]
        assert raw.intent is InterpretedIntent.DENY

    @pytest.mark.parametrize(
        "reply",
        [
            "No, wait",
            "nei, vent",
            "yes but change the date",
            "ja, men endre andre avsnitt",
            "hmm not sure",
            "kanskje",
            "why are you even asking me this",
            "",
            "   ",
        ],
    )
    async def test_hedged_or_unclear_is_ambiguous_never_approve(
        self, interp: LexiconReplyInterpreter, reply: str
    ) -> None:
        raw = await interp.interpret(reply, proposal=None)  # type: ignore[arg-type]
        assert raw.intent is InterpretedIntent.AMBIGUOUS
        assert raw.intent is not InterpretedIntent.APPROVE


class TestLexiconThroughFloorIsSafe:
    """End-to-end: the dangerous replies can never resolve to APPROVE through the floor."""

    pytestmark = pytest.mark.asyncio

    @pytest.mark.parametrize(
        "reply", ["No, wait", "nei, vent", "yes but not 1500kr", "", "kanskje"]
    )
    @pytest.mark.parametrize("clarifications_used", [0, 1])
    async def test_dangerous_replies_never_approve(
        self, reply: str, clarifications_used: int
    ) -> None:
        raw = await LexiconReplyInterpreter().interpret(reply, proposal=None)  # type: ignore[arg-type]
        out = resolve_reply(
            raw, original_arguments=_ORIGINAL, clarifications_used=clarifications_used
        )
        assert out.outcome is not DecisionType.APPROVE
        assert out.outcome in (DecisionType.CLARIFY, DecisionType.DENY)


# --- the A6 chat-twin decision cue (deterministic, reuses the same lexicon) ------------------


@pytest.mark.parametrize(
    ("reply", "is_cue"),
    [
        # affirmatives / negatives → a decision reply (route to the resolver)
        ("yes", True),
        ("no", True),
        ("approve", True),
        ("deny it", True),
        ("ja", True),
        ("nei", True),
        ("godkjenn", True),
        ("yes please", True),  # filler stripped
        # explicit change/edit markers → a decision reply (the user is engaging the approval)
        ("change the amount to 100", True),
        ("modify the recipient", True),
        ("endre beløpet", True),
        # off-topic / non-decision → NOT a cue → a normal chat turn, proposal stays pending
        ("what's the weather in Bergen?", False),
        ("tell me about germany", False),
        ("how are you today", False),
        ("", False),
    ],
)
def test_is_decision_cue(reply: str, is_cue: bool) -> None:
    from persona.approvals import is_decision_cue

    assert is_decision_cue(reply) is is_cue
