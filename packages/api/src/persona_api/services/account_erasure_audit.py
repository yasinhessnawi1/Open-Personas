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
from collections.abc import Mapping
from typing import TYPE_CHECKING

from persona.audit import jsonl_file_lock
from persona.tools._sandbox import require_path_segment
from sqlalchemy import select, update

from persona_api.db.models import store_audit_events as store_audit_t
from persona_api.services.erasure_audit import FREE_TEXT_FIELDS
from persona_api.services.erasure_service import AUDIT_CONTENT_KEYS

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Connection

__all__ = ["rekey_store_audit_file", "rekey_store_audit_rows"]

_FILE_ATTEMPTS = 3


def rekey_store_audit_rows(conn: Connection, *, owner_id: str, erasure_id: str) -> int:
    """The owner's store-audit rows, under the erasure id, without free text or content keys."""
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
    return int(result.rowcount or 0)


def _rekeyed_line(line: str, *, owner_id: str, erasure_id: str) -> str:
    try:
        event = json.loads(line)
    except ValueError:
        return line
    if not isinstance(event, dict):
        return line
    if event.get("persona_id") == owner_id:
        event = {**event, "persona_id": erasure_id, **dict.fromkeys(FREE_TEXT_FIELDS)}
    meta = event.get("metadata")
    if isinstance(meta, dict):
        event = {
            **event,
            "metadata": {k: v for k, v in meta.items() if k not in AUDIT_CONTENT_KEYS},
        }
    return json.dumps(event, separators=(",", ":"), ensure_ascii=False)


def rekey_store_audit_file(audit_root: Path | None, *, owner_id: str, erasure_id: str) -> int:
    """Move ``{owner}.jsonl`` to ``{erasure_id}.jsonl``, each event re-keyed; the lines moved.

    Held under the loggers' lock on the owner's file. A file that keeps growing while it is
    read is retried a few times (a writer in another process), as the persona scrub does. A
    resumed run finds no owner file and moves nothing; an erasure file already there (from a
    run cut short after writing it) is appended to rather than overwritten.
    """
    if audit_root is None:
        return 0
    source = audit_root / f"{require_path_segment(owner_id, what='audit log')}.jsonl"
    target = audit_root / f"{require_path_segment(erasure_id, what='audit log')}.jsonl"
    with jsonl_file_lock(source):
        for _ in range(_FILE_ATTEMPTS):
            if not source.exists():
                return 0
            before = source.read_bytes()
            text = before.decode("utf-8", errors="surrogateescape")
            lines = [line for line in text.split("\n") if line != ""]
            moved = [
                _rekeyed_line(line, owner_id=owner_id, erasure_id=erasure_id) for line in lines
            ]
            if source.read_bytes() != before:  # an event was appended meanwhile: start again
                continue
            payload = ("\n".join(moved) + "\n").encode("utf-8", errors="surrogateescape")
            with target.open("ab") as out:
                out.write(payload)
            source.unlink()
            return len(moved)
    msg = "the owner's audit log kept growing while it was moved"
    raise OSError(msg)
