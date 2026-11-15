"""The store audit after an erasure: ids and labels stay, free text goes (R9-284).

D-R9-284-erase ruling 5 keeps the store audit because it is ids and labels. Its ``reason`` is
not: a persona's own writes record a free-text rationale there, and the graph store copies the
model's ``provenance.reason`` into it. So the erased persona's store-audit rows keep every id,
label and count and lose ``reason``, in the database and in the JSONL files. The graph's events
ride the owner's id in their ``persona_id`` slot (CSA-1) and name the persona in ``written_by``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from persona.tools._sandbox import require_path_segment
from sqlalchemy import and_, or_, update

from persona_api.db.models import store_audit_events as store_audit_t

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Connection

__all__ = ["FREE_TEXT_FIELDS", "scrub_store_audit_files", "scrub_store_audit_rows"]

#: The store-audit fields that can hold free text rather than ids and labels.
FREE_TEXT_FIELDS = ("reason",)

_FILE_ATTEMPTS = 3


def scrub_store_audit_rows(conn: Connection, *, owner_id: str, persona_id: str) -> int:
    """Clear free text from the persona's store-audit rows, and its graph events'."""
    about_the_persona = or_(
        store_audit_t.c.persona_id == persona_id,
        and_(store_audit_t.c.persona_id == owner_id, store_audit_t.c.written_by == persona_id),
    )
    result = conn.execute(
        update(store_audit_t)
        .where(about_the_persona, store_audit_t.c.reason.is_not(None))
        .values(reason=None)
    )
    return int(result.rowcount)


def _scrubbed_line(line: str, *, owner_id: str, persona_id: str) -> str:
    try:
        event = json.loads(line)
    except ValueError:
        return line
    if not isinstance(event, dict):
        return line
    ours = event.get("persona_id") == persona_id or (
        event.get("persona_id") == owner_id and event.get("written_by") == persona_id
    )
    if not ours or all(event.get(f) is None for f in FREE_TEXT_FIELDS):
        return line
    return json.dumps({**event, **dict.fromkeys(FREE_TEXT_FIELDS)}, separators=(",", ":"))


def _scrub_file(path: Path, *, owner_id: str, persona_id: str) -> int:
    """Rewrite one log without the persona's free text. Retried if it grew meanwhile."""
    for _ in range(_FILE_ATTEMPTS):
        if not path.exists():
            return 0
        before = path.read_bytes()
        lines = before.decode("utf-8").splitlines()
        clean = [_scrubbed_line(line, owner_id=owner_id, persona_id=persona_id) for line in lines]
        changed = sum(a != b for a, b in zip(lines, clean, strict=True))
        if not changed:
            return 0
        temporary = path.with_name(path.name + ".erasing")
        temporary.write_bytes(("\n".join(clean) + "\n").encode("utf-8"))
        if path.read_bytes() != before:  # an event was appended meanwhile: start again
            temporary.unlink()
            continue
        temporary.replace(path)
        return changed
    msg = "the audit log kept growing while it was scrubbed"
    raise OSError(msg)


def scrub_store_audit_files(audit_root: Path | None, *, owner_id: str, persona_id: str) -> int:
    """Clear free text from the persona's JSONL store audit and its owner's graph events."""
    if audit_root is None:
        return 0
    return sum(
        _scrub_file(
            audit_root / f"{require_path_segment(name, what='audit log')}.jsonl",
            owner_id=owner_id,
            persona_id=persona_id,
        )
        for name in (persona_id, owner_id)
    )
