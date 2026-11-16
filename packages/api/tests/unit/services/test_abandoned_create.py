"""A create whose process died before its undo is hidden, then erased (R9-293 review HIGH-2).

The personas row is inserted with ``create_complete`` false and set true only once the
identity is indexed. If the process dies between the commit and the undo, the row stays false:
every owner-facing read hides it, and the persona erasure sweep claims its erasure once it is
older than ``ABANDONED_CREATE_GRACE`` (and not before: a younger one may still be finishing).
Runs on the legacy community edition. A "crash" is a failing registry with no undo to run.
"""

from __future__ import annotations

from collections.abc import Iterator  # noqa: TC003 - a fixture annotation
from datetime import UTC, datetime, timedelta
from pathlib import Path  # noqa: TC003 - a fixture annotation
from typing import Any

import pytest
from _erase_seed import OWNER, Seeded, legacy_edition
from persona.errors import PersonaNotFoundError
from persona_api.db.models import personas as personas_t
from persona_api.jobs.handlers.persona_erase import ABANDONED_CREATE_GRACE, PersonaEraseSweep
from persona_api.services import persona_service
from persona_api.services.erasure_ledger import ErasureLedger, SubjectKind
from sqlalchemy import select

_YAML = """\
schema_version: "1.0"
identity:
  name: Astrid
  role: Norwegian tenancy law assistant
  background: |
    Helps tenants understand husleieloven.
  language_default: en
self_facts:
  - fact: Specialised in Norwegian residential tenancy.
    confidence: 1.0
worldview: []
"""


@pytest.fixture
def seeded(tmp_path: Path, embedder: object) -> Iterator[Seeded]:
    with legacy_edition(tmp_path, embedder) as legacy:  # type: ignore[arg-type]
        yield legacy


def _create(seeded: Seeded, embedder: object) -> str:
    return persona_service.create_persona(
        rls_engine=seeded.engine,
        embedder=embedder,  # type: ignore[arg-type]
        audit_root=seeded.audit_root,
        owner_id=OWNER,
        yaml_str=_YAML,
        memory_backend=seeded.memory,
        audit_logger=seeded.audit,
    )


def _crashed_create(seeded: Seeded, embedder: object, monkeypatch: pytest.MonkeyPatch) -> str:
    """The row commits, then the process 'dies': the registry fails and no undo runs."""
    before = _rows(seeded)

    def _dies(*_a: Any) -> Any:  # noqa: ANN401
        msg = "the process died here"
        raise SystemError(msg)

    monkeypatch.setattr(persona_service, "_build_registry", _dies)
    with pytest.raises(SystemError):
        _create(seeded, embedder)
    monkeypatch.undo()
    (pid,) = set(_rows(seeded)) - set(before)
    return pid


def _rows(seeded: Seeded) -> dict[str, bool]:
    with seeded.engine.connect() as conn:
        return {
            str(r.id): bool(r.create_complete)
            for r in conn.execute(select(personas_t.c.id, personas_t.c.create_complete)).all()
        }


def _sweep(seeded: Seeded, *, at: datetime) -> None:
    PersonaEraseSweep(
        engine=seeded.engine,
        run=lambda _erasure, _token: None,
        find_leftovers=lambda *_a, **_k: [],
        clock=lambda: at,
    )()


def test_a_crashed_create_leaves_a_hidden_row(
    seeded: Seeded, embedder: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = _crashed_create(seeded, embedder, monkeypatch)

    assert _rows(seeded)[pid] is False, "the row is there, not complete"
    listed = persona_service.list_personas(rls_engine=seeded.engine, limit=500, offset=0)
    assert pid not in {str(p["id"]) for p in listed}
    with pytest.raises(PersonaNotFoundError):
        persona_service.get_persona(rls_engine=seeded.engine, persona_id=pid)


def test_the_sweep_erases_it_after_the_grace_and_not_before(
    seeded: Seeded, embedder: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    complete = {p for p, done in _rows(seeded).items() if done}
    assert complete, "the seed has complete personas, created long before the grace"
    pid = _crashed_create(seeded, embedder, monkeypatch)
    now = datetime.now(UTC)
    ledger = ErasureLedger(seeded.engine)

    _sweep(seeded, at=now + ABANDONED_CREATE_GRACE - timedelta(minutes=1))

    assert pid in _rows(seeded), "it may still be finishing"
    assert ledger.get(SubjectKind.PERSONA, pid) is None

    _sweep(seeded, at=now + ABANDONED_CREATE_GRACE + timedelta(minutes=1))

    assert pid not in _rows(seeded), "claimed, fenced and its row deleted"
    erasure = ledger.get(SubjectKind.PERSONA, pid)
    assert erasure is not None
    assert erasure.owner_id == OWNER
    # Review MEDIUM-1: the sweep runs on the bypass-RLS engine, across every account; only a
    # create that never finished is its business.
    assert complete <= set(_rows(seeded)), "every complete persona is still there"
    assert all(ledger.get(SubjectKind.PERSONA, p) is None for p in complete)


def test_a_create_that_finished_is_complete_and_listed(seeded: Seeded, embedder: object) -> None:
    pid = _create(seeded, embedder)

    assert _rows(seeded)[pid] is True
    listed = persona_service.list_personas(rls_engine=seeded.engine, limit=500, offset=0)
    assert pid in {str(p["id"]) for p in listed}
    assert persona_service.get_persona(rls_engine=seeded.engine, persona_id=pid)["id"] == pid


def test_the_seeded_personas_are_complete(seeded: Seeded) -> None:
    """Rows written without the column (the default) are complete, as every existing row is."""
    assert all(_rows(seeded).values())
