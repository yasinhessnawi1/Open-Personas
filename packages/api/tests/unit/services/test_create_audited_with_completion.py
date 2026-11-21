"""A complete persona is an audited one (R9-293 review round 3, Finding 1).

The route awaits the create's thread. A cancel landing on that await (a shutdown) used to let
the thread finish the persona while the route's later ``persona.create`` audit row never ran.
The audit row is now written in the same transaction as ``create_complete = true``, in the
thread, so it commits with the flag or not at all.

Runs on the legacy community edition.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Iterator  # noqa: TC003 - a fixture annotation
from pathlib import Path  # noqa: TC003 - a fixture annotation
from typing import Any

import pytest
from _erase_seed import OWNER, Seeded, legacy_edition
from persona_api.db.models import audit_log as audit_log_t
from persona_api.db.models import personas as personas_t
from persona_api.services import audit_service, persona_service
from sqlalchemy import select

_YAML = """\
schema_version: "1.0"
identity:
  name: Astrid
  role: assistant
  background: A helper for the audited create test.
"""


@pytest.fixture
def seeded(tmp_path: Path, embedder: object) -> Iterator[Seeded]:
    with legacy_edition(tmp_path, embedder) as legacy:  # type: ignore[arg-type]
        yield legacy


def _create(seeded: Seeded, embedder: object, **extra: Any) -> str:  # noqa: ANN401
    return persona_service.create_persona(
        rls_engine=seeded.engine,
        embedder=embedder,  # type: ignore[arg-type]
        audit_root=seeded.audit_root,
        owner_id=OWNER,
        yaml_str=_YAML,
        memory_backend=seeded.memory,
        audit_logger=seeded.audit,
        **extra,
    )


def _audits(seeded: Seeded, persona_id: str) -> list[tuple[str, str]]:
    with seeded.engine.connect() as conn:
        rows = conn.execute(
            select(audit_log_t.c.user_id, audit_log_t.c.action).where(
                audit_log_t.c.target == persona_id
            )
        ).all()
    return [(str(r.user_id), str(r.action)) for r in rows]


def _complete(seeded: Seeded, persona_id: str) -> bool:
    with seeded.engine.connect() as conn:
        return bool(
            conn.execute(
                select(personas_t.c.create_complete).where(personas_t.c.id == persona_id)
            ).scalar_one()
        )


def test_a_cancel_on_the_routes_await_still_leaves_the_audit_row(
    seeded: Seeded, embedder: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """As the route does: the create runs in a thread, and the awaiting task is cancelled."""
    indexing = threading.Event()
    cancelled = threading.Event()
    build = persona_service._build_registry  # noqa: SLF001

    def _held(*a: Any, **k: Any) -> Any:  # noqa: ANN401
        registry = build(*a, **k)
        load = registry.load_persona

        def _load(persona: Any) -> None:  # noqa: ANN401
            indexing.set()
            assert cancelled.wait(10), "the awaiting task was never cancelled"
            load(persona)

        registry.load_persona = _load
        return registry

    monkeypatch.setattr(persona_service, "_build_registry", _held)
    done: list[str] = []

    def _in_thread() -> str:
        pid = _create(seeded, embedder)
        done.append(pid)
        return pid

    async def _route_cancelled() -> None:
        task = asyncio.ensure_future(asyncio.to_thread(_in_thread))
        await asyncio.to_thread(indexing.wait, 10)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        cancelled.set()

    asyncio.run(_route_cancelled())  # asyncio.run waits for the default executor's threads

    (pid,) = done
    assert _complete(seeded, pid)
    assert _audits(seeded, pid) == [(OWNER, "persona.create")]


def test_an_audit_that_cannot_be_written_undoes_the_create(
    seeded: Seeded, embedder: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The audit row and the flag commit together, so a failed audit fails (and undoes) it."""
    undone: list[str] = []

    def _no_audit(*_a: Any, **_k: Any) -> None:  # noqa: ANN401
        msg = "audit_log is unwritable"
        raise RuntimeError(msg)

    monkeypatch.setattr(audit_service, "record_in", _no_audit)
    with pytest.raises(RuntimeError, match="unwritable"):
        _create(seeded, embedder, compensate=undone.append)

    (pid,) = undone
    assert not _complete(seeded, pid), "the flag rolled back with the audit"
    assert _audits(seeded, pid) == []
