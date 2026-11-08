"""The erasure ledger: what is being erased and how far it got (R9-284).

Erasing a persona or an account takes many steps across the database, stored files and other
stores, and any of them can fail. This is the record that makes that resumable and provable: one
open row per subject, written before anything is removed, outliving the persona and the user it
names. A finished row does not stop a later erasure of the same subject, because the community
owner is always ``local-owner`` and a hosted identity can be provisioned again.

It holds ids, step names and counts, and this door refuses anything else, because a row that
outlives the deletion must not become the place the deleted content survives. The checks are
shapes, not judgement: a step or count name is a short lower-case identifier, a count is a
non-negative integer, a captured value is a list of short ids with no whitespace, and a failure
is a code rather than an exception message (those carry paths).

The caller chooses the engine: the erase job's BYPASSRLS platform engine, since the rows it needs
are invisible to ``persona_app`` once the persona is gone, or the community engine. Each method
is its own transaction, so a step that is recorded stays recorded if the next one fails.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime  # noqa: TC003 - a dataclass field, read at runtime by callers
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlalchemy import case, func, insert, select, update
from sqlalchemy.exc import IntegrityError

from persona_api.db.models import erasures as erasures_t
from persona_api.errors import ErasureRecordError

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection, Engine

__all__ = ["Erasure", "ErasureLedger", "ErasureState", "SubjectKind"]

_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
_ID = re.compile(r"\S{1,200}")


class SubjectKind(StrEnum):
    """What an erasure erases. Mirrors ``erasures_subject_kind_check``."""

    PERSONA = "persona"
    ACCOUNT = "account"


class ErasureState(StrEnum):
    """Where an erasure stands. Mirrors ``erasures_state_check``."""

    PENDING = "pending"
    RUNNING = "running"
    FAILED = "failed"
    COMPLETE = "complete"


@dataclass(frozen=True)
class Erasure:
    """One ledger row, as read."""

    id: str
    owner_id: str
    subject_kind: SubjectKind
    subject_id: str
    state: ErasureState
    captured: dict[str, list[str]]
    counts: dict[str, dict[str, int]]
    error_code: str | None
    requested_at: datetime
    updated_at: datetime
    completed_at: datetime | None


def _refuse(reason: str) -> ErasureRecordError:
    return ErasureRecordError("erasure ledger refused the write", context={"reason": reason})


def _is_name(value: object) -> bool:
    return isinstance(value, str) and _NAME.fullmatch(value) is not None


def _require_name(value: object, reason: str) -> None:
    if not _is_name(value):
        raise _refuse(reason)


def _require_captured(captured: Mapping[str, Sequence[str]]) -> dict[str, list[str]]:
    clean: dict[str, list[str]] = {}
    for name, ids in captured.items():
        _require_name(name, "captured_name")
        if isinstance(ids, str) or not isinstance(ids, Sequence):
            raise _refuse("captured_not_a_list")
        for value in ids:
            if not isinstance(value, str) or _ID.fullmatch(value) is None:
                raise _refuse("captured_not_an_id")
        clean[name] = list(ids)
    return clean


def _require_counts(counts: Mapping[str, int]) -> dict[str, int]:
    clean: dict[str, int] = {}
    for name, count in counts.items():
        _require_name(name, "count_name")
        # bool is an int to Python, and is not a count.
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise _refuse("count_not_a_number")
        clean[name] = count
    return clean


def _to_erasure(row: Any) -> Erasure:  # noqa: ANN401 - a SQLAlchemy Row
    return Erasure(
        id=row.id,
        owner_id=row.owner_id,
        subject_kind=SubjectKind(row.subject_kind),
        subject_id=row.subject_id,
        state=ErasureState(row.state),
        captured=row.captured,
        counts=row.counts,
        error_code=row.error_code,
        requested_at=row.requested_at,
        updated_at=row.updated_at,
        completed_at=row.completed_at,
    )


class ErasureLedger:
    """Reads and writes ``erasures``, holding every write to ids and counts."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def get(self, subject_kind: SubjectKind, subject_id: str) -> Erasure | None:
        """This subject's open erasure, else its latest finished one, else ``None``."""
        finished_last = case((erasures_t.c.state == ErasureState.COMPLETE.value, 1), else_=0)
        with self._engine.connect() as conn:
            row = conn.execute(
                select(erasures_t)
                .where(
                    erasures_t.c.subject_kind == subject_kind.value,
                    erasures_t.c.subject_id == subject_id,
                )
                .order_by(finished_last, erasures_t.c.requested_at.desc(), erasures_t.c.id.desc())
            ).first()
        return None if row is None else _to_erasure(row)

    def unfinished(self) -> list[Erasure]:
        """Every erasure that has not completed, oldest first, for a sweep to resume."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(erasures_t)
                .where(erasures_t.c.state != ErasureState.COMPLETE.value)
                .order_by(erasures_t.c.requested_at, erasures_t.c.id)
            ).all()
        return [_to_erasure(row) for row in rows]

    def claim(
        self,
        *,
        owner_id: str,
        subject_kind: SubjectKind,
        subject_id: str,
        captured: Mapping[str, Sequence[str]],
    ) -> Erasure:
        """Open an erasure of this subject, or return the one already open.

        A repeat keeps what the first call captured: those ids were read while their rows
        existed, and a retry after the rows have gone would capture nothing. A finished erasure
        is not open, so claiming after one starts a new row.
        """
        clean = _require_captured(captured)
        existing = self.get(subject_kind, subject_id)
        if existing is not None and existing.state is not ErasureState.COMPLETE:
            return existing
        try:
            with self._engine.begin() as conn:
                conn.execute(
                    insert(erasures_t).values(
                        id=f"erasure_{uuid.uuid4().hex}",
                        owner_id=owner_id,
                        subject_kind=subject_kind.value,
                        subject_id=subject_id,
                        captured=clean,
                    )
                )
        except IntegrityError:
            # Two requests raced and the other committed first: its row is this erasure.
            pass
        claimed = self.get(subject_kind, subject_id)
        if claimed is None:  # pragma: no cover - the row was just written or found
            raise _refuse("claim_not_visible")
        return claimed

    def record_step(self, erasure_id: str, step: str, counts: Mapping[str, int]) -> None:
        """Record one step's counts, replacing any earlier run of the same step.

        Replacing is what keeps a resumed erasure honest: the same step run twice is reported
        once. Marks the erasure running and clears any earlier failure.
        """
        _require_name(step, "step_name")
        clean = _require_counts(counts)
        with self._engine.begin() as conn:
            row = self._lock_open(conn, erasure_id)
            conn.execute(
                update(erasures_t)
                .where(erasures_t.c.id == erasure_id)
                .values(
                    counts={**row.counts, step: clean},
                    state=ErasureState.RUNNING.value,
                    error_code=None,
                    updated_at=func.now(),
                )
            )

    def fail(self, erasure_id: str, error_code: str) -> None:
        """Mark the erasure failed with a code. Its counts stay, and it can be resumed."""
        _require_name(error_code, "error_code")
        with self._engine.begin() as conn:
            self._lock_open(conn, erasure_id)
            conn.execute(
                update(erasures_t)
                .where(erasures_t.c.id == erasure_id)
                .values(
                    state=ErasureState.FAILED.value,
                    error_code=error_code,
                    updated_at=func.now(),
                )
            )

    def complete(self, erasure_id: str) -> None:
        """Mark the erasure finished. Final, and harmless to repeat."""
        with self._engine.begin() as conn:
            row = self._lock(conn, erasure_id)
            if row.state == ErasureState.COMPLETE.value:
                return
            conn.execute(
                update(erasures_t)
                .where(erasures_t.c.id == erasure_id)
                .values(
                    state=ErasureState.COMPLETE.value,
                    error_code=None,
                    updated_at=func.now(),
                    completed_at=func.now(),
                )
            )

    def _lock(self, conn: Connection, erasure_id: str) -> Any:  # noqa: ANN401 - a SQLAlchemy Row
        row = conn.execute(
            select(erasures_t).where(erasures_t.c.id == erasure_id).with_for_update()
        ).first()
        if row is None:
            raise _refuse("unknown_erasure")
        return row

    def _lock_open(self, conn: Connection, erasure_id: str) -> Any:  # noqa: ANN401 - a SQLAlchemy Row
        row = self._lock(conn, erasure_id)
        if row.state == ErasureState.COMPLETE.value:
            raise _refuse("erasure_already_complete")
        return row
