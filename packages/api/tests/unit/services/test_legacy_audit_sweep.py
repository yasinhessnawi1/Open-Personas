"""The legacy persona audit sweep (R9-290).

A persona deleted before the erasure ledger left its audit files behind with no row anywhere.
The sweep claims an OPEN persona erasure for one whose owner its own log names (the resume then
erases it in full), cleans the files in place where the owner is unknown or ambiguous, and
leaves alone anything with a persona row, a ledger row, or a log written to just now. Runs on
the community engine.
"""

from __future__ import annotations

import json
import os
import time
from typing import TYPE_CHECKING, Any

import pytest
from _erase_seed import OWNER, legacy_edition, row
from persona_api.db.community import create_community_schema, make_community_engine
from persona_api.db.models import erasures as erasures_t
from persona_api.services import legacy_audit_sweep
from persona_api.services.erasure_ledger import ErasureLedger, ErasureState, SubjectKind
from persona_api.services.erasure_service import PersonaEraseDeps, erase_persona
from persona_api.services.legacy_audit_sweep import (
    QUIET_FOR,
    LegacySweepCounts,
    sweep_legacy_audit_files,
)
from sqlalchemy import select, update

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy.engine import Engine

_OWNER = "user_alice"
_OTHER = "user_bob"
_GONE = "persona_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
_LIVE = "persona_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
_SECRET = "what the user said"


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = make_community_engine(tmp_path / "community.db")
    create_community_schema(eng)
    with eng.begin() as conn:
        for owner in (_OWNER, _OTHER):
            row(conn, "users", id=owner, email=f"{owner}@x.test")
    yield eng
    eng.dispose()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "audit"
    path.mkdir()
    return path


def _event(persona_id: str, *, source: str, written_by: str | None) -> str:
    return json.dumps(
        {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "persona_id": persona_id,
            "action": "write",
            "store": "self_facts",
            "source": source,
            "written_by": written_by,
            "reason": _SECRET,
            "chunk_ids": ["c1"],
            "logical_ids": [],
            "metadata": {"verbatim_ask": _SECRET, "to_version": "3"},
        }
    )


def _old(path: Path) -> None:
    then = time.time() - QUIET_FOR - 60
    os.utime(path, (then, then))


def _logs(root: Path, persona_id: str, *writers: str | None) -> tuple[Path, Path]:
    store = root / f"{persona_id}.jsonl"
    store.write_text(
        "\n".join(
            [
                _event(persona_id, source="system", written_by="system"),
                *(_event(persona_id, source="user", written_by=w) for w in writers),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    tools = root / f"{persona_id}.tools.jsonl"
    tools.write_text('{"tool_name":"file_write","resource":"notes.md"}\n', encoding="utf-8")
    _old(store)
    _old(tools)
    return store, tools


def _erasures(engine: Engine) -> list[tuple[str, str, str]]:
    with engine.connect() as conn:
        return [(r.owner_id, r.subject_id, r.state) for r in conn.execute(select(erasures_t)).all()]


def test_a_legacy_persona_with_a_known_owner_gets_an_open_erasure(
    engine: Engine, root: Path
) -> None:
    store, tools = _logs(root, _GONE, _OWNER, "author")

    counts = sweep_legacy_audit_files(engine, root)

    assert counts == LegacySweepCounts(claimed=1)
    assert _erasures(engine) == [(_OWNER, _GONE, ErasureState.PENDING.value)]
    # The resume's full erasure does the files; the sweep only claims.
    assert tools.exists()
    assert _SECRET in store.read_text(encoding="utf-8")


def test_a_persona_with_a_row_is_never_claimed(engine: Engine, root: Path) -> None:
    with engine.begin() as conn:
        row(conn, "personas", id=_LIVE, owner_id=_OWNER, yaml="x: 1")
    store, tools = _logs(root, _LIVE, _OWNER)
    before = store.read_bytes()

    counts = sweep_legacy_audit_files(engine, root)

    assert counts == LegacySweepCounts(kept_known=1)
    assert _erasures(engine) == []
    assert store.read_bytes() == before
    assert tools.exists()


@pytest.mark.parametrize("state", [ErasureState.PENDING, ErasureState.COMPLETE])
def test_a_persona_the_ledger_knows_is_never_claimed_again(
    engine: Engine, root: Path, state: ErasureState
) -> None:
    ledger = ErasureLedger(engine)
    erasure = ledger.claim(
        owner_id=_OWNER, subject_kind=SubjectKind.PERSONA, subject_id=_GONE, captured={}
    )
    with engine.begin() as conn:
        conn.execute(
            update(erasures_t).where(erasures_t.c.id == erasure.id).values(state=state.value)
        )
    store, tools = _logs(root, _GONE, _OWNER)
    before = store.read_bytes()

    counts = sweep_legacy_audit_files(engine, root)

    assert len(_erasures(engine)) == 1, "never claimed again"
    assert tools.exists(), "never cleaned as a leftover"
    if state is ErasureState.PENDING:
        assert counts == LegacySweepCounts(kept_known=1)
        assert store.read_bytes() == before, "its own files step will clean it"
    else:  # complete: the R9-291 backlog (review L1) cleans the kept log, nothing else
        assert counts == LegacySweepCounts(kept_known=1, scrubbed_backlog=1)
        assert _SECRET not in store.read_text(encoding="utf-8")


def test_two_possible_owners_are_refused_and_the_files_cleaned_in_place(
    engine: Engine, root: Path
) -> None:
    store, tools = _logs(root, _GONE, _OWNER, _OTHER)

    counts = sweep_legacy_audit_files(engine, root)

    assert counts == LegacySweepCounts(scrubbed_ambiguous_owner=1)
    assert _erasures(engine) == []
    assert not tools.exists()
    assert _SECRET not in store.read_text(encoding="utf-8")


def test_an_unknown_owner_is_cleaned_in_place_keeping_ids_and_labels(
    engine: Engine, root: Path
) -> None:
    # "author" and a user id with no users row (an erased account) are not owners.
    store, tools = _logs(root, _GONE, "author", "user_gone")

    counts = sweep_legacy_audit_files(engine, root)

    assert counts == LegacySweepCounts(scrubbed_unknown_owner=1)
    assert _erasures(engine) == []
    assert not tools.exists()
    events = [json.loads(line) for line in store.read_text(encoding="utf-8").splitlines()]
    assert all(e["reason"] is None for e in events)
    assert all(e["metadata"] == {"to_version": "3"} for e in events)
    assert all(e["chunk_ids"] == ["c1"] and e["persona_id"] == _GONE for e in events)


def test_a_dry_run_counts_and_changes_nothing(engine: Engine, root: Path) -> None:
    _logs(root, _GONE, _OWNER)
    other = "persona_cccccccccccccccccccccccccccccccc"
    _logs(root, other, "author")
    before = {p.name: p.read_bytes() for p in root.iterdir()}

    counts = sweep_legacy_audit_files(engine, root, dry_run=True)

    assert counts == LegacySweepCounts(claimed=1, scrubbed_unknown_owner=1)
    assert _erasures(engine) == []
    assert {p.name: p.read_bytes() for p in root.iterdir()} == before


def test_a_second_run_claims_and_cleans_nothing(engine: Engine, root: Path) -> None:
    _logs(root, _GONE, _OWNER)
    other = "persona_cccccccccccccccccccccccccccccccc"
    _logs(root, other, "author")
    first = sweep_legacy_audit_files(engine, root)
    assert first == LegacySweepCounts(claimed=1, scrubbed_unknown_owner=1)
    _old(root / f"{other}.jsonl")  # the clean rewrote it just now

    second = sweep_legacy_audit_files(engine, root)

    assert second == LegacySweepCounts(already_clean=1, kept_known=1)
    assert len(_erasures(engine)) == 1


def test_a_log_written_to_just_now_is_left_for_a_later_run(engine: Engine, root: Path) -> None:
    store, _ = _logs(root, _GONE, _OWNER)
    os.utime(store, None)  # a persona being created: its log before its row commits

    counts = sweep_legacy_audit_files(engine, root)

    assert counts == LegacySweepCounts(kept_recent=1)
    assert _erasures(engine) == []


def test_only_persona_logs_are_considered(engine: Engine, root: Path) -> None:
    for name in (f"{_OWNER}.jsonl", "_cli.tools.jsonl", "erasure_abc.jsonl", "notes.txt"):
        (root / name).write_text(_event(_OWNER, source="user", written_by=_OWNER) + "\n")
        _old(root / name)

    assert sweep_legacy_audit_files(engine, root) == LegacySweepCounts()
    assert sweep_legacy_audit_files(engine, None) == LegacySweepCounts()


def test_the_cli_prints_counts_and_never_a_name(
    engine: Engine,
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import persona_api.config  # noqa: PLC0415
    import persona_api.db.engine  # noqa: PLC0415

    _logs(root, _GONE, _OWNER)

    class _Config:
        database_url = "sqlite://"
        audit_root = str(root)

    monkeypatch.setattr(persona_api.config, "APIConfig", _Config)
    monkeypatch.setattr(persona_api.db.engine, "create_db_engine", lambda _url: engine)

    assert legacy_audit_sweep.main(["--dry-run"]) == 0

    out = capsys.readouterr().out
    assert "claimed=1" in out
    assert _GONE not in out
    assert _OWNER not in out
    assert _erasures(engine) == []


def test_the_resume_erases_a_claimed_legacy_persona_in_full(
    tmp_path: Path,
    embedder: Any,  # noqa: ANN401
) -> None:
    """The claim is all the sweep adds: the existing persona erasure finishes it, files too."""
    with legacy_edition(tmp_path, embedder) as seeded:  # type: ignore[arg-type]
        _resume_erases_in_full(seeded)


def _resume_erases_in_full(seeded: Any) -> None:  # noqa: ANN401
    legacy = "persona_dddddddddddddddddddddddddddddddd"
    store, tools = _logs(seeded.audit_root, legacy, OWNER)

    counts = sweep_legacy_audit_files(seeded.engine, seeded.audit_root)

    assert counts.claimed == 1
    erasure = ErasureLedger(seeded.engine).get(SubjectKind.PERSONA, legacy)
    assert erasure is not None
    erase_persona(
        PersonaEraseDeps(
            engine=seeded.engine,
            memory_backend=seeded.memory,
            audit_logger=seeded.audit,
            file_storage=seeded.storage,
            workspace_root=seeded.workspace_root,
            audit_root=seeded.audit_root,
        ),
        erasure,
    )
    done = ErasureLedger(seeded.engine).get(SubjectKind.PERSONA, legacy)
    assert done is not None
    assert done.state is ErasureState.COMPLETE
    assert not tools.exists()
    # The persona erasure's files step clears free text; its ids and labels stay (ruling 5).
    events = [json.loads(line) for line in store.read_text(encoding="utf-8").splitlines()]
    assert all(e["reason"] is None for e in events)
    assert all(e["chunk_ids"] == ["c1"] for e in events)


def test_only_the_users_own_events_name_an_owner(engine: Engine, root: Path) -> None:
    """A system or persona event's ``written_by`` is not who owns the persona."""
    store, _ = _logs(root, _GONE, _OWNER)
    with store.open("a", encoding="utf-8") as out:
        out.write(_event(_GONE, source="system", written_by=_OTHER) + "\n")
        out.write(_event(_GONE, source="persona_self", written_by=_OTHER) + "\n")
    _old(store)

    counts = sweep_legacy_audit_files(engine, root)

    assert counts == LegacySweepCounts(claimed=1)
    assert _erasures(engine) == [(_OWNER, _GONE, ErasureState.PENDING.value)]


def test_an_engine_that_cannot_see_every_row_is_refused_and_nothing_changes(
    engine: Engine, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review H1 b: under row level security every live persona would look like a leftover."""
    store, tools = _logs(root, _GONE, "author")
    before = store.read_bytes()
    monkeypatch.setattr(legacy_audit_sweep, "sees_every_row", lambda _engine: False)

    counts = sweep_legacy_audit_files(engine, root)

    assert counts == LegacySweepCounts(refused=1)
    assert tools.exists()
    assert store.read_bytes() == before
    assert _erasures(engine) == []


@pytest.mark.parametrize(("granted", "sees"), [(True, True), (False, False), (None, False)])
def test_on_postgres_the_role_must_be_a_superuser_or_bypass_rls(
    granted: bool | None,  # noqa: FBT001
    sees: bool,  # noqa: FBT001
) -> None:
    from types import SimpleNamespace  # noqa: PLC0415

    asked: list[str] = []

    class _Conn:
        def __enter__(self) -> _Conn:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def execute(self, stmt: object) -> SimpleNamespace:
            asked.append(str(stmt))
            return SimpleNamespace(scalar=lambda: granted)

    fake = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"), connect=_Conn)

    assert legacy_audit_sweep.sees_every_row(fake) is sees  # type: ignore[arg-type]
    assert "rolsuper OR rolbypassrls" in asked[0]


def test_a_claim_that_fails_partway_touches_no_file(
    engine: Engine, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review H1 c: every claim is written before any file is cleaned."""
    first = "persona_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    second = "persona_cccccccccccccccccccccccccccccccc"
    unknown = "persona_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"  # between the two claims, in order
    _logs(root, first, _OWNER)
    _logs(root, second, _OTHER)
    _logs(root, unknown, "author")
    before = {p.name: p.read_bytes() for p in root.iterdir()}
    real = ErasureLedger.claim
    calls: list[str] = []

    def _fails_second(self: ErasureLedger, **kwargs: Any) -> Any:  # noqa: ANN401
        calls.append(kwargs["subject_id"])
        if len(calls) == 2:
            msg = "the database went away"
            raise RuntimeError(msg)
        return real(self, **kwargs)

    monkeypatch.setattr(ErasureLedger, "claim", _fails_second)

    with pytest.raises(RuntimeError, match="went away"):
        sweep_legacy_audit_files(engine, root)

    assert {p.name: p.read_bytes() for p in root.iterdir()} == before, "no file was touched"


def test_an_erased_accounts_events_name_its_erasure_after_the_clean(
    engine: Engine, root: Path
) -> None:
    """Review M1: the owner's account was erased (no users row); its log names the erasure."""
    erased = "user_erased"
    account = ErasureLedger(engine).claim(
        owner_id=erased, subject_kind=SubjectKind.ACCOUNT, subject_id=erased, captured={}
    )
    store, tools = _logs(root, _GONE, erased)

    counts = sweep_legacy_audit_files(engine, root)

    assert counts == LegacySweepCounts(scrubbed_unknown_owner=1)
    assert not tools.exists()
    events = [json.loads(line) for line in store.read_text(encoding="utf-8").splitlines()]
    authors = {e["written_by"] for e in events if e["source"] == "user"}
    assert authors == {account.id}
    assert erased not in store.read_text(encoding="utf-8")


def test_a_dry_run_counts_an_events_rekey_as_a_change(engine: Engine, root: Path) -> None:
    erased = "user_erased"
    ErasureLedger(engine).claim(
        owner_id=erased, subject_kind=SubjectKind.ACCOUNT, subject_id=erased, captured={}
    )
    store = root / f"{_GONE}.jsonl"
    event = json.loads(_event(_GONE, source="user", written_by=erased))
    store.write_text(json.dumps({**event, "reason": None, "metadata": {}}) + "\n", "utf-8")
    _old(store)

    counts = sweep_legacy_audit_files(engine, root, dry_run=True)

    assert counts == LegacySweepCounts(scrubbed_unknown_owner=1)


def test_only_an_account_erasure_rekeys_an_author(engine: Engine, root: Path) -> None:
    """A persona erasure that happens to share the author's id is not the author's account."""
    author = "persona_ffffffffffffffffffffffffffffffff"
    ErasureLedger(engine).claim(
        owner_id=_OWNER, subject_kind=SubjectKind.PERSONA, subject_id=author, captured={}
    )
    store, _ = _logs(root, _GONE, author)

    sweep_legacy_audit_files(engine, root)

    events = [json.loads(line) for line in store.read_text(encoding="utf-8").splitlines()]
    assert {e["written_by"] for e in events if e["source"] == "user"} == {author}


def _complete(engine: Engine, persona_id: str) -> None:
    erasure = ErasureLedger(engine).claim(
        owner_id=_OWNER, subject_kind=SubjectKind.PERSONA, subject_id=persona_id, captured={}
    )
    with engine.begin() as conn:
        conn.execute(
            update(erasures_t)
            .where(erasures_t.c.id == erasure.id)
            .values(state=ErasureState.COMPLETE.value)
        )


def test_a_completed_erasures_kept_log_loses_its_content_keys(engine: Engine, root: Path) -> None:
    """Review L1: the R9-291 backlog, erased before the files step dropped content keys."""
    _complete(engine, _GONE)
    store, _ = _logs(root, _GONE, _OWNER)

    first = sweep_legacy_audit_files(engine, root)
    second = sweep_legacy_audit_files(engine, root)

    assert first == LegacySweepCounts(kept_known=1, scrubbed_backlog=1)
    assert second == LegacySweepCounts(kept_known=1), "nothing left to clean"
    events = [json.loads(line) for line in store.read_text(encoding="utf-8").splitlines()]
    assert all(e["metadata"] == {"to_version": "3"} and e["reason"] is None for e in events)
    assert len(_erasures(engine)) == 1


def test_an_open_erasure_is_left_to_its_own_files_step(engine: Engine, root: Path) -> None:
    ErasureLedger(engine).claim(
        owner_id=_OWNER, subject_kind=SubjectKind.PERSONA, subject_id=_GONE, captured={}
    )
    store, _ = _logs(root, _GONE, _OWNER)
    before = store.read_bytes()

    assert sweep_legacy_audit_files(engine, root) == LegacySweepCounts(kept_known=1)
    assert store.read_bytes() == before


def test_a_dry_run_counts_the_backlog_and_changes_nothing(engine: Engine, root: Path) -> None:
    _complete(engine, _GONE)
    store, _ = _logs(root, _GONE, _OWNER)
    before = store.read_bytes()

    counts = sweep_legacy_audit_files(engine, root, dry_run=True)

    assert counts == LegacySweepCounts(kept_known=1, scrubbed_backlog=1)
    assert store.read_bytes() == before


def test_the_cli_reports_a_failure_by_its_type_only(
    engine: Engine,
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Review L4: no traceback and no message (a message can carry a path); exit 1."""
    import persona_api.config  # noqa: PLC0415
    import persona_api.db.engine  # noqa: PLC0415

    class _Config:
        database_url = "sqlite://"
        audit_root = str(root)

    def _broken(*_a: Any, **_k: Any) -> Any:  # noqa: ANN401
        msg = f"cannot read {root / _GONE}"
        raise OSError(msg)

    monkeypatch.setattr(persona_api.config, "APIConfig", _Config)
    monkeypatch.setattr(persona_api.db.engine, "create_db_engine", lambda _url: engine)
    monkeypatch.setattr(legacy_audit_sweep, "sweep_legacy_audit_files", _broken)

    assert legacy_audit_sweep.main([]) == 1

    out = capsys.readouterr()
    assert "OSError" in out.err
    assert _GONE not in out.err + out.out
