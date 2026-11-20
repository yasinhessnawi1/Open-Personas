"""The owner's own store audit after an account erasure (R9-284 T9, review M1).

The graph's store-audit events ride the owner's id in their ``persona_id`` slot (CSA-1), so an
account's store audit names the account even after every persona is erased: in
``store_audit_events`` rows and in the ``{owner}.jsonl`` file. Like ``audit_log``, they are kept
as ids and labels with the owner's id replaced by the erasure id: free text (``reason``) and
content keys go, and the file moves to ``{erasure_id}.jsonl`` so its name no longer carries the
id either. Both are safe to repeat.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import TYPE_CHECKING

from persona.audit import jsonl_file_lock
from persona.tools._sandbox import require_path_segment
from sqlalchemy import select, update

from persona_api.db.connection_bounds import lift_statement_timeout
from persona_api.db.models import store_audit_events as store_audit_t
from persona_api.services.erasure_audit import FREE_TEXT_FIELDS
from persona_api.services.erasure_service import AUDIT_CONTENT_KEYS

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Connection

__all__ = ["rekey_persona_audit_files", "rekey_store_audit_file", "rekey_store_audit_rows"]

_FILE_ATTEMPTS = 3


def rekey_store_audit_rows(conn: Connection, *, owner_id: str, erasure_id: str) -> int:
    """The owner's store-audit rows, under the erasure id, without free text or content keys."""
    # R9-313: the ``written_by`` UPDATE below has no index to use and the table is never
    # pruned, so it scans every tenant's events, and a statement timeout would fail the erasure
    # on every retry. Lifted for the caller's transaction only. (An index is a follow-up.)
    lift_statement_timeout(conn)
    audit = store_audit_t.c
    for audit_id, meta in conn.execute(
        select(audit.id, audit.metadata).where(audit.persona_id == owner_id)
    ).all():
        clean = meta if isinstance(meta, Mapping) else {}
        kept = {k: v for k, v in clean.items() if k not in AUDIT_CONTENT_KEYS}
        if kept != clean:
            conn.execute(update(store_audit_t).where(audit.id == audit_id).values(metadata=kept))
    result = conn.execute(
        update(store_audit_t)
        .where(audit.persona_id == owner_id)
        .values(persona_id=erasure_id, **dict.fromkeys(FREE_TEXT_FIELDS))
    )
    # Review M-1: events the owner wrote into a persona's log (the initial persona load, a
    # tool consent) name the owner in ``written_by``.
    authored = conn.execute(
        update(store_audit_t).where(audit.written_by == owner_id).values(written_by=erasure_id)
    )
    return int(result.rowcount or 0) + int(authored.rowcount or 0)


def _rekeyed_line(line: str, *, owner_id: str, erasure_id: str) -> str:
    try:
        event = json.loads(line)
    except ValueError:
        return line
    if not isinstance(event, dict):
        return line
    if event.get("persona_id") == owner_id:
        event = {**event, "persona_id": erasure_id, **dict.fromkeys(FREE_TEXT_FIELDS)}
    if event.get("written_by") == owner_id:  # review M-1
        event = {**event, "written_by": erasure_id}
    meta = event.get("metadata")
    if isinstance(meta, dict):
        event = {
            **event,
            "metadata": {k: v for k, v in meta.items() if k not in AUDIT_CONTENT_KEYS},
        }
    return json.dumps(event, separators=(",", ":"), ensure_ascii=False)


def _fsync_write(path: Path, payload: bytes, *, append: bool) -> None:
    with path.open("ab" if append else "wb") as out:
        out.write(payload)
        out.flush()
        os.fsync(out.fileno())


def rekey_store_audit_file(audit_root: Path | None, *, owner_id: str, erasure_id: str) -> int:
    """Move ``{owner}.jsonl`` to ``{erasure_id}.jsonl``, each event re-keyed; the lines moved.

    Crash safe and exact (review L-1, round 3 LOW-1/2), under the loggers' lock:

    1. ``{owner}.jsonl`` is renamed to ``{erasure_id}.jsonl.moving`` in the same directory
       (atomic: the owner's name is gone at once, and new events start a new file);
    2. before anything is appended, the target's current size is written to
       ``{erasure_id}.jsonl.moving.offset`` (temporary file, fsync, rename);
    3. the re-keyed lines are appended to ``{erasure_id}.jsonl`` and fsynced;
    4. the ``.moving`` file is removed, then the offset file.

    A resume that finds a ``.moving`` file finishes it before looking at the owner's name: if
    its offset file exists the target is first cut back to that size, so a torn or repeated
    tail never survives and every event lands exactly once, identical lines included. A
    ``.moving`` file freshly made from the owner's log starts with no offset file (a stale one,
    left by a crash between the two removals, is dropped).
    """
    if audit_root is None:
        return 0
    source = audit_root / f"{require_path_segment(owner_id, what='audit log')}.jsonl"
    target = audit_root / f"{require_path_segment(erasure_id, what='audit log')}.jsonl"
    moving = target.with_name(target.name + ".moving")
    offset = target.with_name(target.name + ".moving.offset")
    moved = 0
    with jsonl_file_lock(source):
        for _ in range(_FILE_ATTEMPTS):
            if not moving.exists():
                if not source.exists():
                    offset.unlink(missing_ok=True)  # stale: its .moving is already gone
                    _fsync_dir(audit_root)
                    return moved
                offset.unlink(missing_ok=True)
                _fsync_dir(audit_root)  # the stale offset is gone for good before the rename
                source.replace(moving)
                _fsync_dir(audit_root)
            moved += _land(moving, target, offset, owner_id=owner_id, erasure_id=erasure_id)
            moving.unlink()
            _fsync_dir(audit_root)  # .moving is gone for good before its offset goes
            offset.unlink(missing_ok=True)
            _fsync_dir(audit_root)
            if not source.exists():
                return moved
    msg = "the owner's audit log kept being written while it was moved"
    raise OSError(msg)


def _land(moving: Path, target: Path, offset: Path, *, owner_id: str, erasure_id: str) -> int:
    """Append the ``.moving`` lines, re-keyed, to the target from its recorded offset."""
    text = moving.read_bytes().decode("utf-8", errors="surrogateescape")
    lines = [
        _rekeyed_line(line, owner_id=owner_id, erasure_id=erasure_id)
        for line in text.split("\n")
        if line != ""
    ]
    if offset.exists():  # a run died after recording it: cut back to where it started
        start = int(offset.read_text(encoding="ascii"))
        if target.exists():
            # Never past the end (round 4 LOW-3): truncate would pad the file with NUL bytes.
            start = min(start, target.stat().st_size)
            with target.open("r+b") as out:
                out.truncate(start)
                out.flush()
                os.fsync(out.fileno())
        elif start != 0:
            # Only a first move can record 0 and die before the append creates the target;
            # anything else means the target was lost, and appending would hide that.
            msg = "the recorded offset names an erasure log that is gone"
            raise OSError(msg)
        # A first move that died before its append (review round 4 MEDIUM-1): no target yet,
        # an offset of 0, and the append below creates it.
    else:
        start = target.stat().st_size if target.exists() else 0
        temporary = offset.with_name(offset.name + ".tmp")
        _fsync_write(temporary, str(start).encode("ascii"), append=False)
        temporary.replace(offset)
        _fsync_dir(offset.parent)
    _fsync_write(target, _payload(lines), append=True)
    return len(lines)


def _fsync_dir(directory: Path) -> None:
    """Make a rename in ``directory`` durable; a no-op where the OS cannot (Windows)."""
    if os.name == "nt":
        return
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _payload(lines: list[str]) -> bytes:
    return ("\n".join(lines) + "\n").encode("utf-8", errors="surrogateescape")


def _rekey_file_in_place(path: Path, *, owner_id: str, erasure_id: str) -> int:
    """Rewrite one log with the owner's id replaced, under the loggers' lock; lines changed.

    The lock keeps this process's appends out until the replace (review round 3 LOW-3). A
    writer in another process does not take it; the size check before the replace catches one
    that appended meanwhile, and a write landing between that check and the replace is the
    window the persona scrub (erasure_audit) accepts too (the reviewer's M8).
    """
    with jsonl_file_lock(path):
        for _ in range(_FILE_ATTEMPTS):
            if not path.exists():
                return 0
            before = path.read_bytes()
            text = before.decode("utf-8", errors="surrogateescape")
            lines = text.split("\n")
            if lines and lines[-1] == "":
                lines.pop()
            clean = [
                _rekeyed_line(line, owner_id=owner_id, erasure_id=erasure_id) for line in lines
            ]
            changed = sum(a != b for a, b in zip(lines, clean, strict=True))
            if not changed:
                return 0
            temporary = path.with_name(path.name + ".rekeying")
            _fsync_write(temporary, _payload(clean), append=False)
            if path.read_bytes() != before:  # an event was appended meanwhile: start again
                temporary.unlink()
                continue
            temporary.replace(path)
            _fsync_dir(path.parent)
            return changed
    msg = "a persona's audit log kept growing while it was re-keyed"
    raise OSError(msg)


def rekey_persona_audit_files(
    audit_root: Path | None, persona_ids: list[str], *, owner_id: str, erasure_id: str
) -> int:
    """The owner's id in the account's ``{persona}.jsonl`` logs, replaced (review M-1).

    The persona erasure keeps those logs (ids and labels, ruling 5), and an event the owner
    wrote there names the owner in ``written_by``.
    """
    if audit_root is None:
        return 0
    return sum(
        _rekey_file_in_place(
            audit_root / f"{require_path_segment(pid, what='audit log')}.jsonl",
            owner_id=owner_id,
            erasure_id=erasure_id,
        )
        for pid in persona_ids
    )
