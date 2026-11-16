"""Audit files of personas deleted before the erasure ledger existed (R9-290).

A persona deleted before R9-284 left its ``{persona}.jsonl`` store log and its
``{persona}.tools.jsonl`` tool log in the audit root, with no persona row and no ledger row to
say so. This one-off sweep finds them and hands each to the existing erasure:

* a persona id with neither a ``personas`` row nor an ``erasures`` row (in any state), whose
  files have not been written to within :data:`QUIET_FOR` (a persona being created right now
  has its log before its row commits), is a legacy leftover;
* its owner is read from its store log: the ``written_by`` of the events the owner wrote there
  (``source`` ``user``: the initial persona load, a tool consent), kept only where a ``users``
  row has that id. Exactly one such owner, and an OPEN persona erasure is claimed for it; the
  resume (``PersonaEraseSweep``) then runs the full persona erasure, the files step included,
  and with it any memory, graph or ``memory_chunks`` rows the persona left (memory_chunks has
  no foreign key to personas, so those outlived the row too);
* no such owner, or more than one (refused: the sweep never guesses), and there is no owner to
  put on a ledger row. The files are cleaned where they lie with the persona erasure's own file
  functions: the tool log is removed and the store log loses its free text and content keys.

Safe to repeat: a claimed persona has a ledger row and is skipped from then on, and a cleaned
log has nothing left to clean. It reports counts only, never a file name or an owner id.

It trusts nothing it cannot see (review H1). It runs only on an engine that sees every row
(a superuser or ``BYPASSRLS``; any other refuses with an OPERATOR ALERT and changes nothing),
since under row level security every live persona would look like a leftover. And it decides
first and acts second: every claim is written before any file is touched, so a claim that
fails leaves every file as it was.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, fields
from pathlib import Path
from typing import TYPE_CHECKING

from persona.logging import get_logger
from sqlalchemy import select, text

from persona_api.db.models import erasures as erasures_t
from persona_api.db.models import personas as personas_t
from persona_api.db.models import users as users_t
from persona_api.services.erasure_audit import scrub_persona_log_in_place
from persona_api.services.erasure_ledger import ErasureLedger, ErasureState, SubjectKind
from persona_api.services.erasure_service import AUDIT_CONTENT_KEYS, _remove_tool_log
from persona_api.workspace_paths import is_persona_id

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from sqlalchemy.engine import Engine

__all__ = [
    "QUIET_FOR",
    "LegacySweepCounts",
    "legacy_persona_ids",
    "main",
    "owner_candidates",
    "sees_every_row",
    "sweep_legacy_audit_files",
]

_log = get_logger("api.erasure")

#: A log written to more recently than this may belong to a persona whose row has not committed.
QUIET_FOR = 3600.0

_TOOLS = ".tools.jsonl"
_STORE = ".jsonl"
#: What a legacy persona's claim captures: nothing, its rows went with the old delete.
_CAPTURED: dict[str, list[str]] = {
    "conversations": [],
    "tasks": [],
    "runs": [],
    "schedules": [],
    "event_triggers": [],
}


@dataclass(frozen=True)
class LegacySweepCounts:
    """What one sweep did (or, in a dry run, would do). Counts only."""

    claimed: int = 0
    refused: int = 0
    scrubbed_unknown_owner: int = 0
    scrubbed_ambiguous_owner: int = 0
    already_clean: int = 0
    kept_recent: int = 0
    kept_known: int = 0
    scrubbed_backlog: int = 0

    def lines(self) -> list[str]:
        return [f"{f.name}={getattr(self, f.name)}" for f in fields(self)]


def legacy_persona_ids(audit_root: Path) -> list[str]:
    """Every persona id that names a store or tool log in the audit root."""
    if not audit_root.is_dir():
        return []
    found: set[str] = set()
    for entry in audit_root.iterdir():
        name = entry.name
        if name.endswith(_TOOLS):
            stem = name[: -len(_TOOLS)]
        elif name.endswith(_STORE):
            stem = name[: -len(_STORE)]
        else:
            continue
        if is_persona_id(stem) and entry.is_file():
            found.add(stem)
    return sorted(found)


def _unknown_to_the_database(engine: Engine, ids: list[str]) -> list[str]:
    """The ids with no persona row and no erasure of any state."""
    if not ids:
        return []
    with engine.connect() as conn:
        rows = set(conn.execute(select(personas_t.c.id).where(personas_t.c.id.in_(ids))).scalars())
        erased = set(
            conn.execute(
                select(erasures_t.c.subject_id).where(
                    erasures_t.c.subject_kind == SubjectKind.PERSONA.value,
                    erasures_t.c.subject_id.in_(ids),
                )
            ).scalars()
        )
    return [pid for pid in ids if pid not in rows and pid not in erased]


def _erased_before(engine: Engine, ids: list[str]) -> list[str]:
    """The ids whose persona erasure completed and none is open, with no persona row (L1).

    Their kept logs predate R9-291 and may still hold content keys; cleaning them again is
    a no-op once they hold none.
    """
    if not ids:
        return []
    with engine.connect() as conn:
        rows = set(conn.execute(select(personas_t.c.id).where(personas_t.c.id.in_(ids))).scalars())
        states: dict[str, set[str]] = {}
        for subject, state in conn.execute(
            select(erasures_t.c.subject_id, erasures_t.c.state).where(
                erasures_t.c.subject_kind == SubjectKind.PERSONA.value,
                erasures_t.c.subject_id.in_(ids),
            )
        ).all():
            states.setdefault(str(subject), set()).add(str(state))
    return [
        pid for pid in ids if pid not in rows and states.get(pid) == {ErasureState.COMPLETE.value}
    ]


def owner_candidates(store_log: Path, persona_id: str) -> set[str]:
    """Who wrote into the persona's log as its user: the ``written_by`` of its user events."""
    if not store_log.is_file():
        return set()
    found: set[str] = set()
    text = store_log.read_bytes().decode("utf-8", errors="surrogateescape")
    for line in text.split("\n"):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if (
            isinstance(event, dict)
            and event.get("persona_id") == persona_id
            and event.get("source") == "user"
            and isinstance(event.get("written_by"), str)
        ):
            found.add(event["written_by"])
    return found


def _users(engine: Engine, candidates: set[str]) -> set[str]:
    if not candidates:
        return set()
    with engine.connect() as conn:
        return set(
            conn.execute(select(users_t.c.id).where(users_t.c.id.in_(sorted(candidates)))).scalars()
        )


_SEES_EVERY_ROW = text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user")


def sees_every_row(engine: Engine) -> bool:
    """Whether ``engine``'s role sees every row: a superuser or ``BYPASSRLS`` (review H1 b).

    Off Postgres (the legacy SQLite edition) there is no row level security to hide a row.
    """
    if engine.dialect.name != "postgresql":
        return True
    with engine.connect() as conn:
        return bool(conn.execute(_SEES_EVERY_ROW).scalar())


def _account_erasures(engine: Engine, candidates: set[str]) -> dict[str, str]:
    """``{account id: its erasure id}`` for candidates whose account was erased (review M1).

    The account's own logs name its erasure id instead of it; a legacy persona's log is made
    to match. The latest erasure of an account wins.
    """
    if not candidates:
        return {}
    with engine.connect() as conn:
        rows = conn.execute(
            select(erasures_t.c.subject_id, erasures_t.c.id)
            .where(
                erasures_t.c.subject_kind == SubjectKind.ACCOUNT.value,
                erasures_t.c.subject_id.in_(sorted(candidates)),
            )
            .order_by(erasures_t.c.requested_at, erasures_t.c.id)
        ).all()
    return {str(subject): str(erasure) for subject, erasure in rows}


def _quiet(audit_root: Path, persona_id: str, now: float) -> bool:
    for name in (f"{persona_id}{_STORE}", f"{persona_id}{_TOOLS}"):
        path = audit_root / name
        if path.is_file() and now - path.stat().st_mtime < QUIET_FOR:
            return False
    return True


def _has_content(audit_root: Path, persona_id: str, rekey: dict[str, str]) -> bool:
    """Whether an in-place clean would change anything (the dry run's question)."""
    if (audit_root / f"{persona_id}{_TOOLS}").is_file():
        return True
    return _store_log_has_content(audit_root, persona_id, rekey)


def _store_log_has_content(audit_root: Path, persona_id: str, rekey: dict[str, str]) -> bool:
    store_log = audit_root / f"{persona_id}{_STORE}"
    if not store_log.is_file():
        return False
    for line in store_log.read_bytes().decode("utf-8", errors="surrogateescape").split("\n"):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("persona_id") != persona_id:
            continue
        meta = event.get("metadata")
        if (
            event.get("reason") is not None
            or (isinstance(meta, dict) and AUDIT_CONTENT_KEYS & meta.keys())
            or event.get("written_by") in rekey
        ):
            return True
    return False


def _clean_in_place(audit_root: Path, persona_id: str, rekey: dict[str, str]) -> bool:
    removed = _remove_tool_log(audit_root, persona_id)
    lines = scrub_persona_log_in_place(
        audit_root, persona_id=persona_id, content_keys=AUDIT_CONTENT_KEYS, rekey=rekey
    )
    return bool(removed or lines)


def sweep_legacy_audit_files(
    engine: Engine,
    audit_root: Path | None,
    *,
    dry_run: bool = False,
    clock: Callable[[], float] = time.time,
) -> LegacySweepCounts:
    """Claim an erasure for each legacy persona with a known owner; clean the rest in place.

    Refuses (``refused=1``, an OPERATOR ALERT, nothing changed) on an engine that cannot see
    every row. Decides first, then writes every claim, then cleans files: a claim that fails
    raises before any file is touched.
    """
    if audit_root is None:
        return LegacySweepCounts()
    if not sees_every_row(engine):
        _log.error(
            "OPERATOR ALERT: legacy audit sweep refused: its database role is neither a "
            "superuser nor BYPASSRLS, so it cannot tell a live persona from a leftover; "
            "nothing was changed"
        )
        return LegacySweepCounts(refused=1)
    ids = legacy_persona_ids(audit_root)
    leftovers = _unknown_to_the_database(engine, ids)
    now = clock()
    counts = {f.name: 0 for f in fields(LegacySweepCounts)}
    counts["kept_known"] = len(ids) - len(leftovers)
    claims: list[tuple[str, str]] = []
    cleans: list[tuple[str, bool, dict[str, str]]] = []
    for pid in leftovers:  # decide: reads only
        if not _quiet(audit_root, pid, now):
            counts["kept_recent"] += 1
            continue
        candidates = owner_candidates(audit_root / f"{pid}{_STORE}", pid)
        owners = _users(engine, candidates)
        if len(owners) == 1:
            claims.append((pid, next(iter(owners))))
        else:
            rekey = {} if owners else _account_erasures(engine, candidates)
            cleans.append((pid, bool(owners), rekey))
    if not dry_run:  # the database first: a claim that fails stops the sweep here
        ledger = ErasureLedger(engine)
        for pid, owner in claims:
            ledger.claim(
                owner_id=owner,
                subject_kind=SubjectKind.PERSONA,
                subject_id=pid,
                captured=_CAPTURED,
            )
    counts["claimed"] = len(claims)
    for pid in _erased_before(engine, ids):  # the R9-291 backlog (review L1)
        changed = (
            _store_log_has_content(audit_root, pid, {})
            if dry_run
            else bool(
                scrub_persona_log_in_place(
                    audit_root, persona_id=pid, content_keys=AUDIT_CONTENT_KEYS
                )
            )
        )
        counts["scrubbed_backlog"] += int(changed)
    for pid, ambiguous, rekey in cleans:  # then the files
        changed = (
            _has_content(audit_root, pid, rekey)
            if dry_run
            else _clean_in_place(audit_root, pid, rekey)
        )
        if not changed:
            counts["already_clean"] += 1
        elif ambiguous:
            counts["scrubbed_ambiguous_owner"] += 1
        else:
            counts["scrubbed_unknown_owner"] += 1
    return LegacySweepCounts(**counts)


def run_legacy_sweep_logged(engine: Engine, audit_root: Path | None) -> None:
    """The startup run: the sweep, its counts logged, a failure logged and never raised."""
    try:
        counts = sweep_legacy_audit_files(engine, audit_root)
    except Exception as exc:  # noqa: BLE001 - must not stop the process; logged loudly
        _log.error("legacy audit sweep failed error={err}", err=type(exc).__name__)
        return
    if counts != LegacySweepCounts(kept_known=counts.kept_known):
        _log.warning("legacy audit sweep {counts}", counts=" ".join(counts.lines()))


def main(argv: Iterable[str] | None = None) -> int:
    """``python -m persona_api.services.legacy_audit_sweep [--dry-run]``: counts on stdout."""
    from persona_api.config import APIConfig  # noqa: PLC0415 - the CLI's own config read
    from persona_api.db.engine import create_db_engine  # noqa: PLC0415

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="count only, change nothing")
    parser.add_argument("--audit-root", type=Path, default=None)
    args = parser.parse_args(list(argv) if argv is not None else None)
    config = APIConfig()
    if not config.database_url:
        print("DATABASE_URL is not set", file=sys.stderr)  # noqa: T201 - a CLI
        return 2
    engine = create_db_engine(config.database_url)
    try:
        counts = sweep_legacy_audit_files(
            engine, args.audit_root or Path(config.audit_root), dry_run=args.dry_run
        )
    except Exception as exc:  # noqa: BLE001 - the type only (review L4): a message can carry paths
        print(f"legacy audit sweep failed: {type(exc).__name__}", file=sys.stderr)  # noqa: T201
        return 1
    finally:
        engine.dispose()
    for line in counts.lines():
        print(line)  # noqa: T201 - a CLI
    return 1 if counts.refused else 0


if __name__ == "__main__":
    raise SystemExit(main())
