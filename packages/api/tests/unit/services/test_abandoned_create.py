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


def test_a_create_that_finished_before_the_lock_is_kept(
    seeded: Seeded, embedder: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review MEDIUM-2, the first race: the sweep read it as abandoned, the create finished,
    then the sweep's claim took the row lock. The row is complete by then: kept, no claim."""
    from persona_api.services.erasure_start import claim_abandoned_create  # noqa: PLC0415
    from sqlalchemy import update  # noqa: PLC0415

    pid = _crashed_create(seeded, embedder, monkeypatch)
    with seeded.engine.begin() as conn:
        conn.execute(update(personas_t).where(personas_t.c.id == pid).values(create_complete=True))

    assert claim_abandoned_create(seeded.engine, owner_id=OWNER, persona_id=pid) is None

    assert _rows(seeded)[pid] is True
    assert ErasureLedger(seeded.engine).get(SubjectKind.PERSONA, pid) is None


def test_a_create_swept_before_it_finished_fails_and_is_undone(
    seeded: Seeded, embedder: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review MEDIUM-2, the second race: the sweep claims and deletes the row while the
    identity is still being indexed. The create's last UPDATE finds no incomplete row, so the
    create fails and its undo runs: it never answers for a persona that is gone."""
    from persona.registry import PersonaRegistry  # noqa: PLC0415
    from persona_api.services.erasure_start import claim_abandoned_create  # noqa: PLC0415

    real_load = PersonaRegistry.load_persona
    swept: list[object] = []

    def _swept_meanwhile(self: PersonaRegistry, persona: Any) -> None:  # noqa: ANN401
        real_load(self, persona)
        swept.append(
            claim_abandoned_create(seeded.engine, owner_id=OWNER, persona_id=persona.persona_id)
        )

    monkeypatch.setattr(PersonaRegistry, "load_persona", _swept_meanwhile)
    undone: list[str] = []

    with pytest.raises(RuntimeError, match="swept before it finished"):
        persona_service.create_persona(
            rls_engine=seeded.engine,
            embedder=embedder,  # type: ignore[arg-type]
            audit_root=seeded.audit_root,
            owner_id=OWNER,
            yaml_str=_YAML,
            memory_backend=seeded.memory,
            audit_logger=seeded.audit,
            compensate=undone.append,
        )

    assert len(swept) == 1
    assert swept[0] is not None, "the sweep claimed it"
    assert len(undone) == 1, "the create's undo ran"


def test_a_create_gone_before_its_claim_is_no_alert(
    seeded: Seeded, embedder: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review LOW-4: a row its own undo (or another sweep) took first is debug, not an alert."""
    from persona_api.jobs.handlers import persona_erase  # noqa: PLC0415
    from persona_api.services import erasure_start  # noqa: PLC0415

    pid = _crashed_create(seeded, embedder, monkeypatch)
    said: dict[str, list[str]] = {"debug": [], "error": []}

    class _Log:
        def debug(self, message: str, **_k: Any) -> None:  # noqa: ANN401
            said["debug"].append(message)

        def error(self, message: str, **_k: Any) -> None:  # noqa: ANN401
            said["error"].append(message)

        def warning(self, message: str, **_k: Any) -> None:  # noqa: ANN401
            said["error"].append(message)

    def _gone(*_a: Any, **_k: Any) -> None:  # noqa: ANN401
        raise PersonaNotFoundError("persona not found", context={"id": pid})

    monkeypatch.setattr(persona_erase, "_log", _Log())
    monkeypatch.setattr(erasure_start, "claim_abandoned_create", _gone)

    _sweep(seeded, at=datetime.now(UTC) + ABANDONED_CREATE_GRACE + timedelta(minutes=1))

    assert any("already gone" in m for m in said["debug"])
    assert not [m for m in said["error"] if "OPERATOR ALERT" in m]


def test_finishing_a_create_is_not_an_update(
    seeded: Seeded, embedder: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The final UPDATE keeps updated_at at the INSERT's stamp (CI's created == updated check)."""
    import time  # noqa: PLC0415

    build = persona_service._build_registry  # noqa: SLF001

    def _slow(*a: Any, **k: Any) -> Any:  # noqa: ANN401
        registry = build(*a, **k)
        load = registry.load_persona

        def _load(persona: Any) -> None:  # noqa: ANN401
            load(persona)
            time.sleep(1.1)  # past the next second: SQLite's now() has second resolution

        registry.load_persona = _load
        return registry

    monkeypatch.setattr(persona_service, "_build_registry", _slow)
    pid = _create(seeded, embedder)
    with seeded.engine.connect() as conn:
        created, updated, complete = conn.execute(
            select(
                personas_t.c.created_at, personas_t.c.updated_at, personas_t.c.create_complete
            ).where(personas_t.c.id == pid)
        ).one()
    assert complete
    assert updated == created


def test_a_create_finishing_under_the_sqlite_delete_keeps_its_row(
    seeded: Seeded, embedder: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 3, Finding 4: SQLite has no FOR UPDATE, so the delete itself requires incomplete.

    The create finishes between the sweep's check and its delete. The row stays, complete, and
    no erasure is left claimed for it.
    """
    from persona_api.services import erasure_start  # noqa: PLC0415
    from sqlalchemy import update  # noqa: PLC0415

    pid = _crashed_create(seeded, embedder, monkeypatch)
    capture = erasure_start.capture_persona

    def _finishes_meanwhile(conn: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        captured = capture(conn, **kwargs)
        with seeded.engine.begin() as other:  # the create's final UPDATE, on its own connection
            other.execute(
                update(personas_t).where(personas_t.c.id == pid).values(create_complete=True)
            )
        return captured

    monkeypatch.setattr(erasure_start, "capture_persona", _finishes_meanwhile)
    claimed = erasure_start.claim_abandoned_create(seeded.engine, owner_id=OWNER, persona_id=pid)

    assert claimed is None
    assert _rows(seeded)[pid] is True, "the finished row is kept"
    assert ErasureLedger(seeded.engine).get(SubjectKind.PERSONA, pid) is None, "nothing claimed"
