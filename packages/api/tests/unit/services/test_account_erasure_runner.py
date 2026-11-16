"""The account erasure runner and its sweep (R9-284 T9)."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from persona_api.config import APIConfig, Edition
from persona_api.db.models import erasures
from persona_api.services import account_erasure_runner as runner_mod
from persona_api.services.account_erasure import AccountEraseDeps
from persona_api.services.clerk_users import ClerkUsers
from persona_api.services.erasure_ledger import ErasureLedger, SubjectKind
from sqlalchemy import create_engine

if TYPE_CHECKING:
    from persona_api.services.erasure_ledger import Erasure
    from sqlalchemy import Engine


@pytest.fixture
def engine() -> Engine:
    eng = create_engine("sqlite://")
    erasures.create(eng)
    return eng


def _deps(engine: Engine) -> AccountEraseDeps:
    return AccountEraseDeps(
        persona=SimpleNamespace(engine=engine),  # type: ignore[arg-type]
        stripe=None,
        clerk=None,
        retention=timedelta(days=1),
    )


def _state(edition: Edition, **config: Any) -> SimpleNamespace:  # noqa: ANN401
    return SimpleNamespace(
        config=APIConfig(edition=edition, **config), stripe_gateway=None, platform_engine=None
    )


def test_off_cloud_there_is_no_account_erasure() -> None:
    assert runner_mod.account_erase_deps_from_state(_state(Edition.community)) is None


def test_on_cloud_the_deps_carry_clerk_and_the_retention(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_mod, "erase_deps_from_state", lambda _s: SimpleNamespace())
    state = _state(
        Edition.cloud,
        clerk_secret_key="sk_test_x",
        billing_retention_years=5,
    )

    deps = runner_mod.account_erase_deps_from_state(state)

    assert deps is not None
    assert isinstance(deps.clerk, ClerkUsers)
    assert deps.stripe is None, "no gateway, no Stripe eraser"
    assert deps.retention >= timedelta(days=365 * 5 + 1), "never shorter than five years"


def test_without_a_platform_engine_there_is_nothing_to_run_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner_mod, "erase_deps_from_state", lambda _s: None)

    assert runner_mod.account_erase_deps_from_state(_state(Edition.cloud)) is None


def _claim(engine: Engine, kind: SubjectKind, subject: str) -> Erasure:
    return ErasureLedger(engine).claim(
        owner_id="user_a", subject_kind=kind, subject_id=subject, captured={}
    )


def test_the_sweep_hands_over_only_unfinished_account_erasures(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[str] = []
    monkeypatch.setattr(runner_mod, "erase_account", lambda _d, e: ran.append(e.subject_id))
    open_account = _claim(engine, SubjectKind.ACCOUNT, "user_a")
    _claim(engine, SubjectKind.PERSONA, "persona_x")
    finished = _claim(engine, SubjectKind.ACCOUNT, "user_b")
    ErasureLedger(engine).complete(finished.id)
    runner = runner_mod.AccountEraseRunner(_deps(engine))

    handed = runner.sweep()
    runner._executor.shutdown(wait=True)  # noqa: SLF001 - let the thread finish

    assert handed == 1
    assert ran == [open_account.subject_id]


def test_a_failed_erasure_is_logged_not_raised(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(_d: object, _e: object) -> None:
        raise RuntimeError("step failed")

    monkeypatch.setattr(runner_mod, "erase_account", _boom)
    runner = runner_mod.AccountEraseRunner(_deps(engine))

    runner._run(_claim(engine, SubjectKind.ACCOUNT, "user_a"))  # noqa: SLF001 - no raise


def test_nothing_is_handed_over_once_closed(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[str] = []
    monkeypatch.setattr(runner_mod, "erase_account", lambda _d, e: ran.append(e.id))
    runner = runner_mod.AccountEraseRunner(_deps(engine))
    runner.close()

    runner.submit(_claim(engine, SubjectKind.ACCOUNT, "user_a"))

    assert ran == []


def test_off_cloud_nothing_is_started() -> None:
    state = _state(Edition.community)

    assert runner_mod.start_account_erasures(state) is None
    assert state.account_erase_runner is None


@pytest.mark.parametrize("value", [None, ""])
def test_an_unset_or_empty_secret_is_no_secret(value: str | None) -> None:
    from pydantic import SecretStr  # noqa: PLC0415

    assert runner_mod.secret_value(None if value is None else SecretStr(value)) is None


def test_boot_warns_for_each_missing_account_deletion_secret() -> None:
    config = APIConfig(edition=Edition.cloud, clerk_secret_key="sk_test_x")

    assert runner_mod.warn_missing_secrets(config) == ["PERSONA_CLERK_WEBHOOK_SECRET"]


def test_a_failing_erasure_backs_off_doubling_up_to_an_hour(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review L4: not every sweep; after SWEEP_SECONDS, then twice as long, capped."""
    now = [1000.0]

    def _boom(_d: object, _e: object) -> None:
        raise RuntimeError("clerk is down")

    monkeypatch.setattr(runner_mod, "erase_account", _boom)
    runner = runner_mod.AccountEraseRunner(_deps(engine), clock=lambda: now[0])
    erasure = _claim(engine, SubjectKind.ACCOUNT, "user_a")

    delays = []
    for _ in range(6):
        runner._run(erasure)  # noqa: SLF001
        assert runner.backing_off(erasure.id)
        delays.append(runner._backoff[erasure.id][0])  # noqa: SLF001
        now[0] += delays[-1]
        assert not runner.backing_off(erasure.id), "free again once the delay has passed"

    assert delays == [300.0, 600.0, 1200.0, 2400.0, 3600.0, 3600.0]


def test_the_sweep_leaves_a_backing_off_erasure_alone(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [1000.0]

    def _boom(_d: object, _e: object) -> None:
        raise RuntimeError("down")

    monkeypatch.setattr(runner_mod, "erase_account", _boom)
    runner = runner_mod.AccountEraseRunner(_deps(engine), clock=lambda: now[0])
    erasure = _claim(engine, SubjectKind.ACCOUNT, "user_a")
    runner._run(erasure)  # noqa: SLF001

    assert runner.sweep() == 0
    now[0] += runner_mod.SWEEP_SECONDS
    assert runner.sweep() == 1
    runner._executor.shutdown(wait=True)  # noqa: SLF001


def test_a_success_clears_the_backoff(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    outcomes = [RuntimeError("once"), None]

    def _flaky(_d: object, _e: object) -> None:
        outcome = outcomes.pop(0)
        if outcome is not None:
            raise outcome

    monkeypatch.setattr(runner_mod, "erase_account", _flaky)
    runner = runner_mod.AccountEraseRunner(_deps(engine), clock=lambda: 0.0)
    erasure = _claim(engine, SubjectKind.ACCOUNT, "user_a")

    runner._run(erasure)  # noqa: SLF001
    runner._run(erasure)  # noqa: SLF001

    assert not runner.backing_off(erasure.id)
