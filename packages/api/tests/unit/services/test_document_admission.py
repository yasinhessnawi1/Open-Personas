"""Per-owner admission to the document parse worker (second security review F2).

Every bound is crossed in its real unit on an injected clock: worker-seconds against the
budget, seconds against the window and the cooldown. Each has its "valid input passes"
side: an owner just inside the budget, or just past the cooldown, is admitted.
"""

from __future__ import annotations

import pytest
from persona_api.document_worker.admission import (
    BUDGET_WINDOW_SECONDS,
    COOLDOWN_SECONDS,
    WORKER_SECONDS_BUDGET,
    DocumentAdmission,
    DocumentParseBudgetError,
    DocumentParseCooldownError,
    DocumentUploadInProgressError,
    DocumentUploadsArrivingError,
)


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


def _admission(clock: _Clock, **settings: float) -> DocumentAdmission:
    return DocumentAdmission(clock=clock, **settings)


def test_the_proposed_numbers() -> None:
    assert WORKER_SECONDS_BUDGET == 180.0
    assert BUDGET_WINDOW_SECONDS == 600.0
    assert COOLDOWN_SECONDS == 60.0


class TestTheWorkerTimeBudget:
    def test_an_owner_just_inside_the_budget_is_admitted(self, clock: _Clock) -> None:
        admission = _admission(clock)
        admission.charge("u1", WORKER_SECONDS_BUDGET - 0.001, hit_limit=False)
        admission.check("u1")

    def test_an_owner_at_the_budget_is_refused_with_when_to_return(self, clock: _Clock) -> None:
        admission = _admission(clock)
        admission.charge("u1", WORKER_SECONDS_BUDGET / 2, hit_limit=False)
        clock.now += 100
        admission.charge("u1", WORKER_SECONDS_BUDGET / 2, hit_limit=False)
        with pytest.raises(DocumentParseBudgetError) as caught:
            admission.check("u1")
        assert caught.value.context["reason"] == "parse_budget_exhausted"
        # The oldest charge leaves the window 500 s from now.
        assert caught.value.context["retry_after_seconds"] == "500"

    def test_the_budget_is_per_owner(self, clock: _Clock) -> None:
        admission = _admission(clock)
        admission.charge("u1", WORKER_SECONDS_BUDGET, hit_limit=False)
        admission.check("u2")

    def test_time_that_leaves_the_window_is_given_back(self, clock: _Clock) -> None:
        admission = _admission(clock)
        admission.charge("u1", WORKER_SECONDS_BUDGET, hit_limit=False)
        clock.now += BUDGET_WINDOW_SECONDS - 1
        with pytest.raises(DocumentParseBudgetError):
            admission.check("u1")
        clock.now += 1
        admission.check("u1")
        assert admission.worker_seconds_used("u1") == 0

    def test_an_upload_is_refused_on_the_budget_too(self, clock: _Clock) -> None:
        admission = _admission(clock)
        admission.charge("u1", WORKER_SECONDS_BUDGET, hit_limit=False)
        with pytest.raises(DocumentParseBudgetError):
            admission.begin_upload("u1")
        assert not admission.upload_in_flight("u1")


class TestTheCooldown:
    def test_a_job_that_hit_a_limit_bars_its_owner(self, clock: _Clock) -> None:
        admission = _admission(clock)
        admission.charge("u1", 2.0, hit_limit=True)
        clock.now += COOLDOWN_SECONDS - 1
        with pytest.raises(DocumentParseCooldownError) as caught:
            admission.check("u1")
        assert caught.value.context == {"reason": "parse_cooldown", "retry_after_seconds": "1"}
        admission.check("u2")

    def test_the_owner_is_admitted_again_once_it_ends(self, clock: _Clock) -> None:
        admission = _admission(clock)
        admission.charge("u1", 2.0, hit_limit=True)
        clock.now += COOLDOWN_SECONDS
        admission.check("u1")

    def test_an_ordinary_job_sets_no_cooldown(self, clock: _Clock) -> None:
        admission = _admission(clock)
        admission.charge("u1", 2.0, hit_limit=False)
        admission.check("u1")


class TestUploadLeases:
    def test_one_upload_per_owner_until_every_hold_is_released(self, clock: _Clock) -> None:
        admission = _admission(clock)
        lease = admission.begin_upload("u1")
        lease.hold()  # a worker job
        with pytest.raises(DocumentUploadInProgressError):
            admission.begin_upload("u1")
        admission.begin_upload("u2").release()
        lease.release()  # the route's hold: the job still holds it
        assert admission.upload_in_flight("u1")
        lease.release()  # the job ends
        assert not admission.upload_in_flight("u1")
        admission.begin_upload("u1").release()

    def test_a_release_past_zero_does_nothing(self, clock: _Clock) -> None:
        admission = _admission(clock)
        lease = admission.begin_upload("u1")
        lease.release()
        lease.release()
        later = admission.begin_upload("u1")
        lease.release()  # the stale lease must not free the new one
        assert admission.upload_in_flight("u1")
        later.release()

    def test_an_abandoned_store_that_never_started_releases_and_never_runs(
        self, clock: _Clock
    ) -> None:
        admission = _admission(clock)
        lease = admission.begin_upload("u1")
        lease.abandon_store()
        assert not admission.upload_in_flight("u1")
        assert lease.start_store() is False

    def test_a_store_that_started_keeps_the_lease_until_it_finishes(self, clock: _Clock) -> None:
        admission = _admission(clock)
        lease = admission.begin_upload("u1")
        assert lease.start_store() is True
        lease.abandon_store()  # the request is gone, but the store is running
        assert admission.upload_in_flight("u1")
        lease.release()  # the store finishes
        assert not admission.upload_in_flight("u1")


class TestReleaseCallbacks:
    """Third review finding 7: the scratch directory goes when the last hold does."""

    def test_a_callback_runs_once_when_the_last_hold_is_released(self, clock: _Clock) -> None:
        admission = _admission(clock)
        lease = admission.begin_upload("u1")
        ran: list[str] = []
        lease.on_released(lambda: ran.append("scratch"))
        lease.hold()  # the worker job
        lease.release()  # the request is gone
        assert ran == []  # the worker may still be writing to it
        lease.release()  # the job ends
        assert ran == ["scratch"]
        lease.release()
        assert ran == ["scratch"]

    def test_a_callback_given_after_the_release_runs_at_once(self, clock: _Clock) -> None:
        admission = _admission(clock)
        lease = admission.begin_upload("u1")
        lease.release()
        ran: list[str] = []
        lease.on_released(lambda: ran.append("now"))
        assert ran == ["now"]

    def test_the_callback_runs_without_the_admission_lock(self, clock: _Clock) -> None:
        admission = _admission(clock)
        lease = admission.begin_upload("u1")
        lease.on_released(lambda: admission.begin_upload("u1").release())
        lease.release()  # would deadlock if the callback ran under the lock
        assert not admission.upload_in_flight("u1")


class TestBodiesArriving:
    """Third review finding 5: at most two upload bodies arriving per owner."""

    def test_a_third_body_is_refused_and_a_released_place_is_reused(self, clock: _Clock) -> None:
        admission = _admission(clock)
        first = admission.begin_body("u1")
        second = admission.begin_body("u1")
        with pytest.raises(DocumentUploadsArrivingError) as caught:
            admission.begin_body("u1")
        assert caught.value.context == {"reason": "uploads_arriving", "retry_after_seconds": "5"}
        admission.begin_body("u2").release()  # per owner
        first.release()
        assert admission.bodies_arriving("u1") == 1
        third = admission.begin_body("u1")
        assert admission.bodies_arriving("u1") == 2
        second.release()
        third.release()
        assert admission.bodies_arriving("u1") == 0

    def test_a_place_is_freed_once_however_often_it_is_released(self, clock: _Clock) -> None:
        admission = _admission(clock)
        first = admission.begin_body("u1")
        second = admission.begin_body("u1")
        first.release()
        first.release()  # the body arrived, then the request ended
        assert admission.bodies_arriving("u1") == 1
        second.release()
        assert admission.bodies_arriving("u1") == 0

    def test_the_cap_is_settable(self, clock: _Clock) -> None:
        admission = DocumentAdmission(clock=clock, max_bodies_per_owner=1)
        admission.begin_body("u1")
        with pytest.raises(DocumentUploadsArrivingError):
            admission.begin_body("u1")


class TestSuspectOwners:
    def test_a_limit_makes_the_owner_suspect_beyond_the_cooldown(self, clock: _Clock) -> None:
        admission = _admission(clock, suspect_seconds=300)
        admission.charge("u1", 1.0, hit_limit=True)
        clock.now += COOLDOWN_SECONDS
        admission.check("u1")  # the cooldown is over
        assert admission.is_suspect("u1")  # but the owner still goes last
        clock.now += 300 - COOLDOWN_SECONDS - 1
        assert admission.is_suspect("u1")
        clock.now += 1
        assert not admission.is_suspect("u1")
