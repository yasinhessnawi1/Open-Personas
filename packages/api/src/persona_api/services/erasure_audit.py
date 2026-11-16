"""The store audit after an erasure: ids and labels stay, free text goes (R9-284).

D-R9-284-erase ruling 5 keeps the store audit because it is ids and labels. Its ``reason`` is
not: a persona's own writes record a free-text rationale there, and the graph store copies the
model's ``provenance.reason`` into it. So the erased persona's store-audit rows keep every id,
label and count and lose ``reason``, in the database and in the JSONL files. The graph's events
ride the owner's id in their ``persona_id`` slot (CSA-1) and name the persona in ``written_by``.
"""

from __future__ import annotations

import json
import os
import secrets
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

from persona.audit import jsonl_file_lock
from persona.tools._sandbox import require_path_segment
from sqlalchemy import and_, or_, select, update

from persona_api.db.models import store_audit_events as store_audit_t

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Connection

__all__ = [
    "FREE_TEXT_FIELDS",
    "scrub_persona_log_in_place",
    "scrub_store_audit_files",
    "scrub_store_audit_rows",
]

#: The store-audit fields that can hold free text rather than ids and labels.
FREE_TEXT_FIELDS = ("reason",)

_FILE_ATTEMPTS = 3


def scrub_store_audit_rows(
    conn: Connection,
    *,
    owner_id: str,
    persona_id: str,
    content_keys: frozenset[str] = frozenset(),
) -> int:
    """Clear free text from the persona's store-audit rows, and its graph events'.

    Their ``metadata`` also loses ``content_keys`` (review L2): no writer puts those keys into
    the store audit today, so this is defence, and the backlog of any that did.
    """
    about_the_persona = or_(
        store_audit_t.c.persona_id == persona_id,
        and_(store_audit_t.c.persona_id == owner_id, store_audit_t.c.written_by == persona_id),
    )
    if content_keys:
        for audit_id, meta in conn.execute(
            select(store_audit_t.c.id, store_audit_t.c.metadata)
            .where(about_the_persona)
            .order_by(store_audit_t.c.id)
        ).all():
            if isinstance(meta, Mapping) and content_keys & meta.keys():
                conn.execute(
                    update(store_audit_t)
                    .where(store_audit_t.c.id == audit_id)
                    .values(metadata={k: v for k, v in meta.items() if k not in content_keys})
                )
    result = conn.execute(
        update(store_audit_t)
        .where(about_the_persona, store_audit_t.c.reason.is_not(None))
        .values(reason=None)
    )
    return int(result.rowcount)


def _scrubbed_line(
    line: str, *, owner_id: str, persona_id: str, content_keys: frozenset[str] = frozenset()
) -> str:
    try:
        event = json.loads(line)
    except ValueError:
        return line
    if not isinstance(event, dict):
        return line
    ours = event.get("persona_id") == persona_id or (
        event.get("persona_id") == owner_id and event.get("written_by") == persona_id
    )
    if not ours:
        return line
    clean = {**event, **dict.fromkeys(FREE_TEXT_FIELDS)}
    meta = event.get("metadata")
    if isinstance(meta, dict):  # review L2, and only a dict (L3)
        clean["metadata"] = {k: v for k, v in meta.items() if k not in content_keys}
    if clean == event:
        return line
    return json.dumps(clean, separators=(",", ":"), ensure_ascii=False)


def _scrub_file(
    path: Path, *, owner_id: str, persona_id: str, content_keys: frozenset[str] = frozenset()
) -> int:
    """Rewrite one log without the persona's free text, holding the loggers' lock (review M-2).

    The lock keeps this process's appends out until the replace; the size check behind it is
    for a writer in another process. Bytes that are not UTF-8 are carried through unchanged
    (surrogateescape), so one bad byte cannot make the step fail forever.
    """
    with jsonl_file_lock(path):
        return _scrub_held_file(
            path,
            lambda line: _scrubbed_line(
                line, owner_id=owner_id, persona_id=persona_id, content_keys=content_keys
            ),
        )


def _scrub_held_file(path: Path, clean_line: Callable[[str], str]) -> int:
    for _ in range(_FILE_ATTEMPTS):
        if not path.exists():
            return 0
        before = path.read_bytes()
        # By newline only: str.splitlines also splits on U+2028 and the like, which JSON leaves
        # raw inside a string, and would cut an event apart (review M-1).
        text = before.decode("utf-8", errors="surrogateescape")
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        clean = [clean_line(line) for line in lines]
        changed = sum(a != b for a, b in zip(lines, clean, strict=True))
        if not changed:
            return 0
        # A name of its own (review L7): two processes rewriting the same log never share a
        # temporary file, and it is on disk before it replaces the log.
        temporary = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(4)}.erasing")
        with temporary.open("wb") as out:
            out.write(("\n".join(clean) + "\n").encode("utf-8", errors="surrogateescape"))
            out.flush()
            os.fsync(out.fileno())
        if path.read_bytes() != before:  # an event was appended meanwhile: start again
            temporary.unlink()
            continue
        temporary.replace(path)
        return changed
    msg = "the audit log kept growing while it was scrubbed"
    raise OSError(msg)


def scrub_store_audit_files(
    audit_root: Path | None,
    *,
    owner_id: str,
    persona_id: str,
    content_keys: frozenset[str] = frozenset(),
) -> int:
    """Clear free text and ``content_keys`` from the persona's JSONL store audit, and from its
    events in the owner's log (the graph's, ``written_by`` the persona; review L2)."""
    if audit_root is None:
        return 0
    return sum(
        _scrub_file(
            audit_root / f"{require_path_segment(name, what='audit log')}.jsonl",
            owner_id=owner_id,
            persona_id=persona_id,
            content_keys=content_keys,
        )
        for name in (persona_id, owner_id)
    )


def _contentless_line(
    line: str,
    *,
    persona_id: str,
    content_keys: frozenset[str],
    rekey: Mapping[str, str] | None = None,
) -> str:
    """The persona's event without free text or content keys; any other line unchanged.

    ``rekey`` maps an erased account's id to its erasure id: an event that account wrote names
    the erasure instead (review M1), as the account erasure's own re-key does.
    """
    try:
        event = json.loads(line)
    except ValueError:
        return line
    if not isinstance(event, dict) or event.get("persona_id") != persona_id:
        return line
    meta = event.get("metadata")
    clean = {**event, **dict.fromkeys(FREE_TEXT_FIELDS)}
    if isinstance(meta, dict):
        # Only a dict is rewritten (review L3): a missing or odd metadata stays as it was, so
        # the line still reads back as the event it was.
        clean["metadata"] = {k: v for k, v in meta.items() if k not in content_keys}
    author = event.get("written_by")
    if rekey and isinstance(author, str) and author in rekey:
        clean["written_by"] = rekey[author]
    if clean == event:
        return line
    return json.dumps(clean, separators=(",", ":"), ensure_ascii=False)


def scrub_persona_log_in_place(
    audit_root: Path,
    *,
    persona_id: str,
    content_keys: frozenset[str],
    rekey: Mapping[str, str] | None = None,
) -> int:
    """Clear the persona's ``{persona}.jsonl`` of free text and content keys; lines changed.

    For a persona whose owner is not known (R9-290): no ledger row can carry its erasure, so
    the log is cleaned where it lies, by the same locked rewrite as the erasure's scrub. Ids,
    labels and counts stay, as ruling 5 keeps them.
    """
    path = audit_root / f"{require_path_segment(persona_id, what='audit log')}.jsonl"
    with jsonl_file_lock(path):
        return _scrub_held_file(
            path,
            lambda line: _contentless_line(
                line, persona_id=persona_id, content_keys=content_keys, rekey=rekey
            ),
        )
