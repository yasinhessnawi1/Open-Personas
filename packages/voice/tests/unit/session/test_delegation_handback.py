"""Unit tests for the voice delegation hand-back poller (Spec A9, T6; A9-D-5/D-7).

The poller resolves a pending delegation off the durable state — the ``delegated_turn`` job's
terminal row (done-signal) + the outcome message (grounded content) — and speaks the right hand-back
at the next idle floor: the grounded content on ``succeeded`` (never a replay of the echo), the
honest-incomplete line on ``blocked_on_approval`` (never a fake done), a fail-soft line on failure.
Polling runs only while pending; a non-terminal job leaves the delegation pending for the next pass.
"""

from __future__ import annotations

from typing import Any

import pytest
from persona_voice.loop.streaming import Transcript
from persona_voice.session.delegation_handback import DelegationHandbackPoller

pytestmark = pytest.mark.asyncio

_KEY = "delegate:call-1:abc123"


class _FakeResult:
    def __init__(self, *, scalar: object = None, row: dict[str, Any] | None = None) -> None:
        self._scalar = scalar
        self._row = row

    def scalar(self) -> object:
        return self._scalar

    def mappings(self) -> _FakeResult:
        return self

    def first(self) -> dict[str, Any] | None:
        return self._row


class _FakeConn:
    def __init__(self, *, job_state: object, outcome: dict[str, Any] | None) -> None:
        self._job_state = job_state
        self._outcome = outcome

    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, _stmt: object, params: dict[str, Any]) -> _FakeResult:
        if "owner" in params:  # the job-state query
            return _FakeResult(scalar=self._job_state)
        return _FakeResult(row=self._outcome)  # the outcome-message query


class _FakeEngine:
    def __init__(self, *, job_state: object, outcome: dict[str, Any] | None = None) -> None:
        self._conn = _FakeConn(job_state=job_state, outcome=outcome)

    def begin(self) -> _FakeConn:
        return self._conn


def _poller(engine: object, sink: object) -> DelegationHandbackPoller:
    return DelegationHandbackPoller(
        engine=engine,  # type: ignore[arg-type]
        owner_id="owner-1",
        conversation_id="call-1",
        on_handback=sink,  # type: ignore[arg-type]
    )


class _Sink:
    def __init__(self) -> None:
        self.narrations: list[Transcript] = []

    async def __call__(self, narration: Transcript) -> None:
        self.narrations.append(narration)


async def test_pending_job_is_not_resolved_and_stays_tracked() -> None:
    sink = _Sink()
    poller = _poller(_FakeEngine(job_state="running"), sink)
    poller.track(_KEY)
    resolved = await poller.poll_once()
    assert resolved == []  # the job is not terminal yet
    assert sink.narrations == []
    await poller.shutdown()


async def test_succeeded_speaks_the_grounded_content() -> None:
    sink = _Sink()
    engine = _FakeEngine(
        job_state="succeeded",
        outcome={
            "content": "Done. I've set that up: track morning fares.",
            "outcome": "succeeded",
        },
    )
    poller = _poller(engine, sink)
    poller.track(_KEY)
    resolved = await poller.poll_once()
    assert resolved == [_KEY]
    assert len(sink.narrations) == 1
    text = sink.narrations[0].text
    assert "track morning fares" in text  # grounded in the REAL outcome, not the echo
    assert "actually done" in text  # the grounded-result framing


async def test_blocked_on_approval_speaks_the_honest_incomplete_line() -> None:
    sink = _Sink()
    honest = "I've started it. It needs your OK in your chat with Astrid."
    engine = _FakeEngine(
        job_state="succeeded",
        outcome={"content": honest, "outcome": "blocked_on_approval"},
    )
    poller = _poller(engine, sink)
    poller.track(_KEY)
    await poller.poll_once()
    text = sink.narrations[0].text
    assert honest in text  # the honest-incomplete line, never a fake done
    assert "approval" in text


async def test_failed_speaks_a_fail_soft_line_without_details() -> None:
    sink = _Sink()
    engine = _FakeEngine(
        job_state="succeeded",
        outcome={"content": "boom stacktrace leak", "outcome": "failed"},
    )
    poller = _poller(engine, sink)
    poller.track(_KEY)
    await poller.poll_once()
    text = sink.narrations[0].text
    assert "could not be completed" in text
    assert "boom stacktrace" not in text  # fail-soft: no internal detail leaks to the caller


async def test_terminal_job_with_no_outcome_message_is_fail_soft() -> None:
    # The executor-error edge: job terminal, no outcome row → treated as failed, never a fake done.
    sink = _Sink()
    poller = _poller(_FakeEngine(job_state="dead", outcome=None), sink)
    poller.track(_KEY)
    resolved = await poller.poll_once()
    assert resolved == [_KEY]
    assert "could not be completed" in sink.narrations[0].text


async def test_resolved_key_is_dropped_and_not_spoken_twice() -> None:
    sink = _Sink()
    engine = _FakeEngine(
        job_state="succeeded", outcome={"content": "Done.", "outcome": "succeeded"}
    )
    poller = _poller(engine, sink)
    poller.track(_KEY)
    await poller.poll_once()
    await poller.poll_once()  # second pass — the key is gone
    assert len(sink.narrations) == 1  # spoken exactly once
    await poller.shutdown()


async def test_narration_is_a_final_transcript() -> None:
    sink = _Sink()
    engine = _FakeEngine(
        job_state="succeeded", outcome={"content": "Done.", "outcome": "succeeded"}
    )
    poller = _poller(engine, sink)
    poller.track(_KEY)
    await poller.poll_once()
    assert sink.narrations[0].is_final is True
    assert sink.narrations[0].confidence == 1.0
