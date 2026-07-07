"""Unit tests — the initiative scan handler (Spec A5, T6).

Deterministic (fake context/scanner/sink): dial-OFF is a handler EXIT (the
scanner is never invoked, the schedule untouched by construction); every scan
fire emits the synthesis-style metering row (credits_charged=0, candidate
count visible); the sink seam is fail-soft; the dial reader's conservative
fallbacks hold.
"""

# ruff: noqa: ARG002 — fakes deliberately ignore some args
from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from typing import Any

import pytest
from persona.initiative import (
    CandidateSource,
    CitationKind,
    GroundingCitation,
    InitiativeCandidate,
    InitiativeDial,
    InitiativeTrigger,
    PlannedStep,
    Urgency,
)
from persona.tools.categories import ActionCategory
from persona_api.initiative.handler import InitiativeScanHandler, InitiativeScanPayload

_FIRE = datetime(2026, 7, 4, 5, 0, tzinfo=UTC)


class _FakeContext:
    def __init__(self, owner_id: str = "u1") -> None:
        self._owner_id = owner_id
        self.meters: list[dict[str, Any]] = []

    @property
    def owner_id(self) -> str:
        return self._owner_id

    @property
    def job_id(self) -> str:
        return "job-1"

    @contextlib.contextmanager
    def connection(self) -> Iterator[object]:
        yield object()

    def meter(
        self, *, amount_micros: int, kind: str, detail: Mapping[str, str] | None = None
    ) -> None:
        self.meters.append(
            {"amount_micros": amount_micros, "kind": kind, "detail": dict(detail or {})}
        )


def _candidate() -> InitiativeCandidate:
    return InitiativeCandidate(
        observation="The hearing is Friday.",
        citations=(GroundingCitation(kind=CitationKind.NODE, ref="node-1"),),
        trigger=InitiativeTrigger.APPROACHING_COMMITMENT,
        why_now="Date entered horizon.",
        plan=(PlannedStep(description="draft", categories=frozenset({ActionCategory.DRAFT})),),
        next_step="Draft it.",
        value=0.9,
        acceptance=0.8,
        urgency=Urgency.BATCH,
        source=CandidateSource.SCAN,
        owner_id="u1",
        persona_id="p1",
        prompt_version="a5-scan-v1",
        scanned_at=_FIRE,
    )


class _FakeScanner:
    def __init__(self, candidates: tuple[InitiativeCandidate, ...] = ()) -> None:
        self._candidates = candidates
        self.calls = 0

    async def scan(
        self,
        owner_id: str,
        persona_id: str,
        *,
        fire_time: datetime,  # noqa: ARG002
    ) -> tuple[InitiativeCandidate, ...]:
        self.calls += 1
        return self._candidates


class _FakeSink:
    def __init__(self, *, raises: bool = False) -> None:
        self._raises = raises
        self.received: list[tuple[InitiativeCandidate, ...]] = []

    async def submit(self, candidates: tuple[InitiativeCandidate, ...]) -> None:
        if self._raises:
            msg = "pipeline down"
            raise RuntimeError(msg)
        self.received.append(candidates)


def _payload() -> InitiativeScanPayload:
    return InitiativeScanPayload(persona_id="p1", schedule_id="initsched:p1", fire_time=_FIRE)


def _handler(
    scanner: _FakeScanner,
    *,
    dial: InitiativeDial = InitiativeDial.PROPOSE_ONLY,
    sink: _FakeSink | None = None,
    pause_check: Callable[[str], bool] | None = None,
) -> InitiativeScanHandler:
    kwargs: dict[str, Any] = {}
    if pause_check is not None:
        kwargs["pause_check"] = pause_check
    return InitiativeScanHandler(
        scanner=scanner, dial_reader=lambda _o, _p: dial, sink=sink, **kwargs
    )


@pytest.mark.asyncio
async def test_dial_off_is_a_handler_exit_scanner_never_invoked() -> None:
    """The Phase-1 ruling: OFF exits the handler; the schedule stays; no spend."""
    scanner = _FakeScanner()
    context = _FakeContext()
    await _handler(scanner, dial=InitiativeDial.OFF).handle(_payload(), context)  # type: ignore[arg-type]
    assert scanner.calls == 0
    assert context.meters == []  # no scan, no metering row — nothing ran


@pytest.mark.asyncio
async def test_owner_autonomy_paused_exits_before_dial_or_spend() -> None:
    """A6-D-8 completeness: a paused owner originates no initiative, even dial-on.

    The pause is checked BEFORE the dial read + scan + metering — a paused owner leaks nothing
    (no scanner call, no metering row, no sink submit). This is the injected origination gate.
    """
    scanner = _FakeScanner((_candidate(),))
    sink = _FakeSink()
    context = _FakeContext(owner_id="paused-owner")
    handler = _handler(
        scanner,
        dial=InitiativeDial.ACT_WITHIN_ENVELOPE,  # dial fully ON — only the pause holds it
        sink=sink,
        pause_check=lambda owner: owner == "paused-owner",
    )
    await handler.handle(_payload(), context)  # type: ignore[arg-type]
    assert scanner.calls == 0  # never scanned
    assert context.meters == []  # no spend row
    assert sink.received == []  # no proposal originated


@pytest.mark.asyncio
async def test_every_scan_fire_emits_the_metering_row() -> None:
    """The tension-5 ruling ADD: synthesis-style, credits_charged=0, count visible."""
    scanner = _FakeScanner(())
    context = _FakeContext()
    await _handler(scanner).handle(_payload(), context)  # type: ignore[arg-type]
    assert len(context.meters) == 1
    row = context.meters[0]
    assert row["amount_micros"] == 0
    assert row["detail"]["surface"] == "initiative_scan"
    assert row["detail"]["candidates"] == "0"


@pytest.mark.asyncio
async def test_candidates_flow_to_the_sink() -> None:
    sink = _FakeSink()
    scanner = _FakeScanner((_candidate(),))
    await _handler(scanner, sink=sink).handle(_payload(), _FakeContext())  # type: ignore[arg-type]
    assert len(sink.received) == 1
    assert sink.received[0][0].opportunity_key == "approaching_commitment:node/node-1"


@pytest.mark.asyncio
async def test_no_sink_is_the_t6_staging_posture_metered_not_delivered() -> None:
    scanner = _FakeScanner((_candidate(),))
    context = _FakeContext()
    await _handler(scanner, sink=None).handle(_payload(), context)  # type: ignore[arg-type]
    assert context.meters[0]["detail"]["candidates"] == "1"  # produced + metered, nothing more


@pytest.mark.asyncio
async def test_sink_failure_degrades_to_silence() -> None:
    scanner = _FakeScanner((_candidate(),))
    context = _FakeContext()
    await _handler(scanner, sink=_FakeSink(raises=True)).handle(_payload(), context)  # type: ignore[arg-type]
    assert context.meters  # metered; the sink error did not propagate (no retry storm)


class TestDialReaderFallbacks:
    @staticmethod
    def _patched_dial(monkeypatch: pytest.MonkeyPatch, row: object) -> InitiativeDial:
        """Run ``read_initiative_dial`` with a canned row (no DB in unit tests)."""
        import persona_api.initiative.handler as handler_mod

        class _Result:
            @staticmethod
            def first() -> object:
                return row

        class _Conn:
            @staticmethod
            def execute(_stmt: object) -> _Result:
                return _Result()

        @contextlib.contextmanager
        def _fake_rls(_engine: object, _owner: str) -> Iterator[object]:
            yield _Conn()

        monkeypatch.setattr(handler_mod, "rls_connection", _fake_rls)
        return handler_mod.read_initiative_dial(object(), "u1", "p1")  # type: ignore[arg-type]

    def test_unknown_stored_value_reads_as_the_conservative_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from persona.initiative import DEFAULT_INITIATIVE_DIAL

        dial = self._patched_dial(monkeypatch, ["warp_speed"])  # a hand-edited row
        assert dial is DEFAULT_INITIATIVE_DIAL  # never act-within-envelope by accident

    def test_missing_persona_reads_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert self._patched_dial(monkeypatch, None) is InitiativeDial.OFF
