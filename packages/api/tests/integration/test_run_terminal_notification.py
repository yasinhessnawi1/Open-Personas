"""Server-authored run-terminal notifications (Spec P6, D4-c).

Drives the REAL persist chokepoints — ``RunRegistry._persist_final`` /
``_persist_error`` — against Docker Postgres, exactly as the background worker
calls them (owner bound via the RLS contextvar). Not a hand-forced notification:
the terminal persist itself must produce the durable bell row. Concerns:

1. A completed run writes a ``run_terminal`` notification (level/key by status)
   with the persona name in ``params`` (locale-neutral copy, P6-D-5).
2. **Idempotent** — a repeated persist (retry / restart) leaves ONE row (P6-D-11).
3. **Best-effort** — when the notification write fails, the run persist still
   commits and no exception escapes (D-P6-12).
4. The error path writes a ``failed`` notification.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from persona_api.background.run_worker import RunRegistry
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from persona_api.services import notifications_service
from persona_runtime.agentic.run import RunStatus
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

_OWNER = "user_rterm_a"
_PERSONA = "persona_rterm_a"
_RUN = "run_rterm_1"
_T0 = datetime(2026, 6, 25, 12, 0, 0, tzinfo=UTC)


def _fake_run(status: RunStatus) -> SimpleNamespace:
    """A minimal stand-in for the loop's ``Run`` (only the fields _persist_final reads)."""
    return SimpleNamespace(
        status=status, steps=[], output="done", error=None, finished_at=_T0
    )


@pytest.fixture
def seeded(migrated_engine: Engine) -> Iterator[Engine]:
    """Seed owner → persona (named) → a running run; yield the superuser engine."""
    with migrated_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:u, :e)"),
            {"u": _OWNER, "e": f"{_OWNER}@example.com"},
        )
        conn.execute(
            text(
                "INSERT INTO personas (id, owner_id, yaml) "
                "VALUES (:p, :o, 'identity:\n  name: Ada')"
            ),
            {"p": _PERSONA, "o": _OWNER},
        )
        conn.execute(
            text(
                "INSERT INTO runs (id, owner_id, persona_id, task, status) "
                "VALUES (:r, :o, :p, 'do a thing', 'running')"
            ),
            {"r": _RUN, "o": _OWNER, "p": _PERSONA},
        )
    yield migrated_engine
    with migrated_engine.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": _OWNER})


@pytest.fixture
def registry() -> Iterator[tuple[RunRegistry, object]]:
    """A registry on the non-superuser app engine, with the owner bound (as the worker does)."""
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")
    engine = make_rls_engine(app_url)
    token = current_user_id.set(_OWNER)
    try:
        yield RunRegistry(engine), token
    finally:
        current_user_id.reset(token)
        engine.dispose()


def _notifications(engine: Engine) -> list[dict[str, object]]:
    with engine.begin() as conn:
        return [
            dict(r)
            for r in conn.execute(
                text("SELECT * FROM notifications WHERE owner_id = :o"), {"o": _OWNER}
            )
            .mappings()
            .all()
        ]


def test_completed_run_writes_run_terminal_notification(
    seeded: Engine, registry: tuple[RunRegistry, object]
) -> None:
    reg, _ = registry
    reg._persist_final(_RUN, _fake_run(RunStatus.COMPLETED))  # noqa: SLF001 — drive the chokepoint
    rows = _notifications(seeded)
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "run_terminal"
    assert row["ref_id"] == _RUN
    assert row["level"] == "success"
    assert row["message_key"] == "notifications.run.completed"
    assert row["params"] == {"persona": "Ada"}
    assert row["read"] is False


def test_run_terminal_notification_is_idempotent(
    seeded: Engine, registry: tuple[RunRegistry, object]
) -> None:
    reg, _ = registry
    reg._persist_final(_RUN, _fake_run(RunStatus.COMPLETED))  # noqa: SLF001
    reg._persist_final(_RUN, _fake_run(RunStatus.COMPLETED))  # noqa: SLF001 — a retry
    assert len(_notifications(seeded)) == 1  # (owner, kind, ref) key → no duplicate


def test_persist_still_commits_when_notification_write_fails(
    seeded: Engine,
    registry: tuple[RunRegistry, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reg, _ = registry

    def _boom(**_kwargs: object) -> None:
        raise RuntimeError("feed down")

    monkeypatch.setattr(notifications_service, "create_notification", _boom)
    # Best-effort: no exception escapes...
    reg._persist_final(_RUN, _fake_run(RunStatus.COMPLETED))  # noqa: SLF001
    # ...the authoritative run persist still committed...
    with seeded.begin() as conn:
        status = conn.execute(
            text("SELECT status FROM runs WHERE id = :r"), {"r": _RUN}
        ).scalar_one()
    assert status == "completed"
    # ...and no notification row was written.
    assert _notifications(seeded) == []


def test_error_run_writes_failed_notification(
    seeded: Engine, registry: tuple[RunRegistry, object]
) -> None:
    reg, _ = registry
    reg._persist_error(_RUN, "boom")  # noqa: SLF001 — drive the error chokepoint
    rows = _notifications(seeded)
    assert len(rows) == 1
    assert rows[0]["level"] == "error"
    assert rows[0]["message_key"] == "notifications.run.failed"
