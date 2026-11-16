"""One run of an erasure at a time, and the lock is always given back (R9-284, review M1).

The in-process lock (the legacy SQLite edition). The Postgres advisory lock is exercised in
tests/integration/test_erase_persona_postgres.py.
"""

from __future__ import annotations

from collections.abc import Iterator  # noqa: TC003 - a fixture annotation
from pathlib import Path  # noqa: TC003 - a fixture annotation

import pytest
from persona_api.db.community import make_community_engine
from persona_api.services.erasure_lock import erasure_lock, lock_key
from sqlalchemy.engine import Engine  # noqa: TC002 - a fixture annotation

_ID = "erasure_" + "a" * 32


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = make_community_engine(tmp_path / "community.db")
    yield eng
    eng.dispose()


def test_a_second_run_does_not_get_the_lock_while_the_first_holds_it(engine: Engine) -> None:
    with erasure_lock(engine, _ID) as first:
        with erasure_lock(engine, _ID) as second:
            assert (first, second) == (True, False)
        with erasure_lock(engine, "erasure_" + "b" * 32) as other:
            assert other is True


def test_the_lock_is_given_back_after_a_run_and_after_a_failure(engine: Engine) -> None:
    with erasure_lock(engine, _ID) as held:
        assert held
    with pytest.raises(RuntimeError), erasure_lock(engine, _ID):
        raise RuntimeError("a step failed")

    with erasure_lock(engine, _ID) as again:
        assert again is True


def test_a_run_that_did_not_get_the_lock_does_not_release_the_holders(engine: Engine) -> None:
    with erasure_lock(engine, _ID) as first:
        with erasure_lock(engine, _ID) as second:
            assert not second
        with erasure_lock(engine, _ID) as third:
            assert (first, third) == (True, False)


def test_the_advisory_key_is_stable_and_distinct() -> None:
    assert lock_key(_ID) == lock_key(_ID)
    assert lock_key(_ID) != lock_key("erasure_" + "b" * 32)
    assert -(2**63) <= lock_key(_ID) < 2**63
