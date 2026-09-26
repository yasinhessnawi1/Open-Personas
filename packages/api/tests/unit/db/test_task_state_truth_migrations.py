"""Migrations 057 and 058 take their locks carefully (R9-158 review and re-review).

``runs`` and ``jobs`` are written on every task leg. A migration that waits for a lock
queues every one of those writes behind itself, so both fail fast instead
(``lock_timeout``), set at the start and reset at the end of every direction so it cannot
leak into a later migration in the same run. 057 adds its CHECK ``NOT VALID`` and validates
it OUTSIDE the migration's transaction, so the scan of existing rows does not hold the
add's exclusive lock. The round trip itself runs on Postgres in
``tests/integration/test_migration_task_state_truth.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_VERSIONS = Path(__file__).resolve().parents[3] / "alembic" / "versions"


def _body(name: str, direction: str) -> str:
    source = (_VERSIONS / name).read_text(encoding="utf-8")
    start = source.index(f"def {direction}")
    end = source.index("def downgrade") if direction == "upgrade" else len(source)
    return re.sub(r"\s+", " ", source[start:end])


def _ddl_positions(body: str) -> list[int]:
    return [m.start() for m in re.finditer(r"ALTER TABLE|CREATE INDEX|DROP INDEX", body)]


@pytest.mark.parametrize("name", ["057_runs_stop_reason.py", "058_jobs_live_task_leg_index.py"])
@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_the_migration_fails_fast_and_leaves_no_timeout_behind(name: str, direction: str) -> None:
    body = _body(name, direction)
    ddl = _ddl_positions(body)
    assert ddl, "no DDL found: the scan is broken"
    assert "SET LOCAL" not in body  # a session setting, reset explicitly
    assert 0 <= body.find("SET lock_timeout = '5s'") < ddl[0]
    assert body.find("RESET lock_timeout") > ddl[-1]


def test_057_validates_its_check_outside_the_transaction_that_added_it() -> None:
    body = _body("057_runs_stop_reason.py", "upgrade")
    added = body.index("ADD CONSTRAINT runs_stop_reason_check")
    not_valid = body.index("NOT VALID", added)
    outside = body.index("autocommit_block()", not_valid)
    validated = body.index("VALIDATE CONSTRAINT runs_stop_reason_check")
    assert added < not_valid < outside < validated
