"""D-ORCH-11-18-u: the per-conversation "fine to bring this up" control, as a service.

Only the conversation's owner reads or changes it (an unknown or another owner's conversation
is not found). It reads ``held`` when a disclosure keeps the conversation out of initiative,
``released`` while the user's release holds and ``not_held`` otherwise, with the disclosure
count the user saw as its ``version``. A release is written only for a held conversation and
only for the count the user saw; a re-block always clears it.
"""

# ruff: noqa: ARG002 (duck-typed fakes)
from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from persona_api.errors import ConversationNotFoundError
from persona_api.initiative.conversation_hold import (
    ConversationHold,
    ConversationHoldService,
    HoldChangedError,
    version_token,
)
from persona_api.initiative.disclosures import DisclosureState, ReleaseOutcome
from persona_api.initiative.wellbeing_gate import ConversationWellbeingGate

if TYPE_CHECKING:
    from collections.abc import Iterable

_AT = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
_CREATED = datetime(2026, 11, 1, 8, 30, 12, 345678, tzinfo=UTC)


class _Graph:
    def flagged_nodes(self, owner_id: str) -> list[Any]:
        return []


class _Facts:
    def __init__(self, state: DisclosureState | None, outcome: ReleaseOutcome) -> None:
        self.state = state
        self.outcome = outcome
        self.calls: list[tuple[Any, ...]] = []

    def disclosed(self, owner_id: str, refs: Iterable[str]) -> set[str]:
        return set(refs) if self.state is not None else set()

    def states(self, owner_id: str, refs: Iterable[str]) -> dict[str, DisclosureState]:
        return dict.fromkeys(refs, self.state) if self.state is not None else {}

    def release(
        self, owner_id: str, persona_id: str, interaction_id: str, *, version: int, at: datetime
    ) -> ReleaseOutcome:
        self.calls.append(("release", owner_id, persona_id, interaction_id, version, at))
        if self.outcome is ReleaseOutcome.CHANGED and self.state is not None:
            seq = self.state.disclosure_seq
            self.state = replace(self.state, released_seq=seq, released_at=at)
        return self.outcome

    def hold(self, owner_id: str, persona_id: str, interaction_id: str) -> ReleaseOutcome:
        self.calls.append(("hold", owner_id, persona_id, interaction_id))
        if self.state is not None:
            self.state = replace(self.state, released_seq=None, released_at=None)
        return ReleaseOutcome.CHANGED


_HELD = DisclosureState(disclosure_seq=2, released_seq=None, released_at=None, created_at=_CREATED)
_RELEASED = replace(_HELD, released_seq=2, released_at=_AT)
_OVERTAKEN = replace(_HELD, disclosure_seq=3, released_seq=2, released_at=_AT)


def _tok(state: DisclosureState | None) -> str:
    return version_token("u", "conv-d", state)


def _service(
    monkeypatch: pytest.MonkeyPatch,
    state: DisclosureState | None,
    outcome: ReleaseOutcome = ReleaseOutcome.CHANGED,
    *,
    conversations: dict[tuple[str, str], str] | None = None,
) -> tuple[ConversationHoldService, _Facts]:
    facts = _Facts(state, outcome)
    gate = ConversationWellbeingGate(_Graph(), object(), disclosures=facts, releases=facts)  # type: ignore[arg-type]
    monkeypatch.setattr(gate, "unsynthesised", lambda _o, _ids: set())
    owned = {("u", "conv-d"): "pers"} if conversations is None else conversations
    service = ConversationHoldService(
        _Graph(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        facts=facts,  # type: ignore[arg-type]
        gate=gate,
        persona_of=lambda owner, conv: owned.get((owner, conv)),
    )
    return service, facts


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (_HELD, ConversationHold(state="held", released_at=None, version=_tok(_HELD))),
        (_RELEASED, ConversationHold(state="released", released_at=_AT, version=_tok(_HELD))),
        (_OVERTAKEN, ConversationHold(state="held", released_at=None, version=_tok(_OVERTAKEN))),
        (None, ConversationHold(state="not_held", released_at=None, version=_tok(None))),
    ],
)
def test_it_reads_the_state_and_the_count_the_user_sees(
    monkeypatch: pytest.MonkeyPatch, state: DisclosureState | None, expected: ConversationHold
) -> None:
    service, _ = _service(monkeypatch, state)
    assert service.read("u", "conv-d") == expected


def test_a_conversation_that_is_not_the_callers_is_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutant 8: the owner check is the conversation lookup, for reads and writes."""
    service, facts = _service(monkeypatch, _HELD)
    with pytest.raises(ConversationNotFoundError):
        service.read("someone_else", "conv-d")
    with pytest.raises(ConversationNotFoundError):
        service.change("someone_else", "conv-d", released=True, version=_tok(_HELD), at=_AT)
    with pytest.raises(ConversationNotFoundError):
        service.change("someone_else", "conv-d", released=False, version=None, at=_AT)
    assert facts.calls == []


def test_a_held_conversation_is_released_for_the_count_seen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, facts = _service(monkeypatch, _HELD)
    got = service.change("u", "conv-d", released=True, version=_tok(_HELD), at=_AT)
    assert facts.calls == [("release", "u", "pers", "conv-d", 2, _AT)], "the store gets the count"
    assert got == ConversationHold(state="released", released_at=_AT, version=_tok(_HELD))


def test_a_conversation_not_held_is_never_written(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutant 11: a release of nothing would plant a fact row the user never needed."""
    service, facts = _service(monkeypatch, None)
    got = service.change("u", "conv-d", released=True, version=_tok(None), at=_AT)
    assert facts.calls == []
    assert got.state == "not_held"


def test_a_release_in_force_is_not_written_again(monkeypatch: pytest.MonkeyPatch) -> None:
    service, facts = _service(monkeypatch, _RELEASED)
    got = service.change("u", "conv-d", released=True, version=_tok(_RELEASED), at=_AT)
    assert got.state == "released"
    assert facts.calls == []


def test_a_count_that_moved_is_a_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    service, _ = _service(monkeypatch, _HELD, ReleaseOutcome.CONFLICT)
    with pytest.raises(HoldChangedError):
        service.change("u", "conv-d", released=True, version=_tok(_HELD), at=_AT)


def test_a_persona_gone_meanwhile_is_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    service, _ = _service(monkeypatch, _HELD, ReleaseOutcome.GONE)
    with pytest.raises(ConversationNotFoundError):
        service.change("u", "conv-d", released=True, version=_tok(_HELD), at=_AT)


def test_a_release_needs_the_count_the_user_saw(monkeypatch: pytest.MonkeyPatch) -> None:
    service, facts = _service(monkeypatch, _HELD)
    with pytest.raises(ValueError, match="version"):
        service.change("u", "conv-d", released=True, version=None, at=_AT)
    assert facts.calls == []


def test_a_re_block_holds_it_back_again(monkeypatch: pytest.MonkeyPatch) -> None:
    service, facts = _service(monkeypatch, _RELEASED)
    got = service.change("u", "conv-d", released=False, version=None, at=_AT)
    assert facts.calls == [("hold", "u", "pers", "conv-d")]
    assert got == ConversationHold(state="held", released_at=None, version=_tok(_HELD))


class _NetGraph:
    """A graph that gains a tagged node citing the conversation after the release."""

    def __init__(self) -> None:
        self.nodes: list[Any] = []

    def flagged_nodes(self, owner_id: str) -> list[Any]:
        return self.nodes


class _StoreLike(_Facts):
    """The store's branch order: a moved count is a conflict, otherwise (re)stamp."""

    def release(
        self, owner_id: str, persona_id: str, interaction_id: str, *, version: int, at: datetime
    ) -> ReleaseOutcome:
        self.calls.append(("release", owner_id, persona_id, interaction_id, version, at))
        assert self.state is not None
        if self.state.disclosure_seq != version:
            return ReleaseOutcome.CONFLICT
        seq = self.state.disclosure_seq
        self.state = replace(self.state, released_seq=seq, released_at=at)
        return ReleaseOutcome.CHANGED


def test_a_release_overtaken_by_a_tagged_write_can_be_given_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR #44 review MEDIUM-1 (the reviewer's probe): it used to stay held for good."""
    from datetime import timedelta  # noqa: PLC0415

    from persona.graph.models import ConceptNode, NodeKind, NodeProvenance  # noqa: PLC0415
    from persona.schema.chunks import WriteSource  # noqa: PLC0415

    graph = _NetGraph()
    facts = _StoreLike(
        DisclosureState(disclosure_seq=1, released_seq=None, released_at=None),
        ReleaseOutcome.CHANGED,
    )
    gate = ConversationWellbeingGate(graph, object(), disclosures=facts, releases=facts)  # type: ignore[arg-type]
    monkeypatch.setattr(gate, "unsynthesised", lambda _o, _ids: set())
    service = ConversationHoldService(
        graph,  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        facts=facts,  # type: ignore[arg-type]
        gate=gate,
        persona_of=lambda _o, _c: "pers",
    )
    first = service.read("u", "conv")
    assert first.state == "held"
    assert service.change("u", "conv", released=True, version=first.version, at=_AT).state == (
        "released"
    )
    graph.nodes.append(
        ConceptNode(
            id="n1",
            node_kind=NodeKind.FACT,
            concept_name="x",
            content="x",
            wellbeing_category="mental_health_crisis",
            provenance=(
                NodeProvenance(
                    source=WriteSource.PERSONA_SELF,
                    interaction_id="conv",
                    written_at=_AT + timedelta(minutes=5),
                ),
            ),
            created_at=_AT,
        )
    )
    seen = service.read("u", "conv")
    assert seen.state == "held", "the net holds it again"
    later = _AT + timedelta(hours=1)
    again = service.change("u", "conv", released=True, version=seen.version, at=later)
    assert again == ConversationHold(state="released", released_at=later, version=seen.version)


def test_the_version_is_an_opaque_token_that_names_no_count() -> None:
    """PR #44 review LOW-1: the count is not readable from the token, nor guessable."""
    token = _tok(_HELD)
    assert re.fullmatch(r"v1\.[0-9a-f]{32}", token)
    guesses = {
        version_token(
            "u", "conv-d", DisclosureState(disclosure_seq=n, released_seq=None, released_at=None)
        )
        for n in range(100)
    } | {_tok(None)}
    assert token not in guesses, "without the row's own creation time no count guesses it"
    assert _tok(replace(_HELD, disclosure_seq=3)) != token, "a new disclosure changes it"
    assert version_token("u", "conv-e", _HELD) != token
    assert version_token("someone_else", "conv-d", _HELD) != token
    assert _tok(_RELEASED) == token, "a release alone does not move it"


def test_a_stale_token_is_a_conflict_and_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    service, facts = _service(monkeypatch, _HELD)
    stale = _tok(replace(_HELD, disclosure_seq=1))
    with pytest.raises(HoldChangedError):
        service.change("u", "conv-d", released=True, version=stale, at=_AT)
    assert facts.calls == []
