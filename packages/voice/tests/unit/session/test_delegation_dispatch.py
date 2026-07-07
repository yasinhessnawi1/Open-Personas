"""Unit tests for the voice delegation dispatcher (Spec A9, T10 — fail-soft).

On a successful enqueue the dispatcher registers the delegation with the hand-back poller (so the
result comes back); on an enqueue FAILURE it fails soft — it does NOT track (no infinite poll) and
speaks the "couldn't set it up" line. No half-created state is possible either way: voice never
creates anything; the create is the worker's, keyed idempotently.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from persona_voice.model.origination_gate import DelegatedTurnIntent
from persona_voice.session.delegation_dispatch import DelegationDispatcher

pytestmark = pytest.mark.asyncio


@dataclass
class _FakePoller:
    tracked: list[str]

    def track(self, delegation_key: str) -> None:
        self.tracked.append(delegation_key)


class _RaisingEngine:
    """An engine whose ``begin()`` raises — the enqueue-failure path."""

    def begin(self) -> object:
        raise RuntimeError("db down")


class _R:
    def first(self) -> tuple[str]:
        return ("job-1",)


class _Conn:
    def __enter__(self) -> _Conn:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, _stmt: object, _params: dict[str, object]) -> _R:
        return _R()


class _OkEngine:
    """An engine that records the enqueue and returns a job id."""

    def begin(self) -> _Conn:
        return _Conn()


def _intent() -> DelegatedTurnIntent:
    return DelegatedTurnIntent(
        conversation_id="call-1", verbatim_ask="remind me every morning", persona_id="p1"
    )


async def test_successful_enqueue_tracks_the_hand_back() -> None:
    failed: list[bool] = []

    async def _on_failed() -> None:
        failed.append(True)

    poller = _FakePoller(tracked=[])
    dispatcher = DelegationDispatcher(
        engine=_OkEngine(),  # type: ignore[arg-type]
        owner_id="owner-1",
        poller=poller,  # type: ignore[arg-type]
        on_failed=_on_failed,
    )
    dispatcher.dispatch(_intent())
    await dispatcher.join()  # awaits the in-flight dispatch task
    # a job landed → the delegation is tracked for the hand-back; the fail-soft never fired
    assert len(poller.tracked) == 1
    assert failed == []


async def test_enqueue_failure_fails_soft_and_does_not_track() -> None:
    failed: list[bool] = []

    async def _on_failed() -> None:
        failed.append(True)

    poller = _FakePoller(tracked=[])
    dispatcher = DelegationDispatcher(
        engine=_RaisingEngine(),  # type: ignore[arg-type]
        owner_id="owner-1",
        poller=poller,  # type: ignore[arg-type]
        on_failed=_on_failed,
    )
    dispatcher.dispatch(_intent())
    await dispatcher.join()
    # the enqueue failed → the user is told (fail-soft) and NOTHING is tracked (no infinite poll);
    # no half-created state is possible (the durable job never landed).
    assert failed == [True]
    assert poller.tracked == []
