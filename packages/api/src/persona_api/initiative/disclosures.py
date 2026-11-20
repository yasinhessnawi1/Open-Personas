"""The durable wellbeing disclosure fact, over ``wellbeing_disclosures`` (R9-253).

Synthesis and the direct write record that an interaction carried a wellbeing disclosure;
the initiative wellbeing gate reads it. The fact holds the owner, the persona, the
interaction id and a time, never the disclosed text or its category (D-ORCH-11-18-m), and no
node or conversation deletion clears it: only the persona's or the account's erasure does
(both foreign keys cascade).

Every statement runs in its own short owner-scoped transaction (``rls_connection``), so a
fact written ahead of a merge stays written whatever happens to the merge: the gate errs on
the side of holding back. Logs carry ids only.

The user's "fine to bring this up" release lives on the same row (D-ORCH-11-18-u, migration
066). Each recorded disclosure bumps ``disclosure_seq``; a release copies the count the user
saw into ``released_seq``, so a disclosure recorded later holds the conversation back again.
The record and the release lock the same row, so they are serialized, and a counter rather
than a clock decides which came later. A release or a re-block writes one neutral audit row
in the same transaction (``conversation.setting_changed``, the conversation, the persona id):
no state and no counter, so the audit marks no disclosure and the erasure rulings apply to
it unchanged.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from persona.logging import get_logger
from sqlalchemy import insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from persona_api.db.engine import rls_connection
from persona_api.db.models import audit_log as audit_log_t
from persona_api.db.models import personas as personas_t
from persona_api.db.models import wellbeing_disclosures

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime

    from sqlalchemy import ColumnElement, Engine, Insert, Select

__all__ = [
    "SETTING_CHANGED_ACTION",
    "DisclosureState",
    "PgDisclosureStore",
    "ReleaseOutcome",
]

_log = get_logger("api.initiative.disclosures")

#: The one neutral audit action for a release and a re-block alike (D-ORCH-11-18-u).
SETTING_CHANGED_ACTION = "conversation.setting_changed"

_T = wellbeing_disclosures.c


@dataclass(frozen=True)
class DisclosureState:
    """A fact row's counters: the disclosures recorded and the count a release covers."""

    disclosure_seq: int
    released_seq: int | None
    released_at: datetime | None
    #: When the fact was first recorded. Never shown: it keys the control's opaque version.
    created_at: datetime | None = None

    @property
    def release_in_force(self) -> bool:
        """Whether a release covers every disclosure recorded so far."""
        return (
            self.released_seq is not None
            and self.released_at is not None
            and self.disclosure_seq <= self.released_seq
        )


class ReleaseOutcome(StrEnum):
    """What a release or a re-block did."""

    CHANGED = "changed"
    UNCHANGED = "unchanged"
    #: The count the user saw is not the count now: a disclosure landed in between.
    CONFLICT = "conflict"
    #: The persona no longer exists for this owner: nothing was written.
    GONE = "gone"


def _persona_lock(owner_id: str, persona_id: str) -> Select[tuple[str]]:
    """The persona row, held ``FOR KEY SHARE`` so an erasure waits for this commit."""
    return (
        select(personas_t.c.id)
        .where(personas_t.c.id == persona_id, personas_t.c.owner_id == owner_id)
        .with_for_update(read=True, key_share=True)
    )


def _fact(owner_id: str, interaction_id: str) -> tuple[ColumnElement[bool], ...]:
    return (_T.owner_id == owner_id, _T.interaction_id == interaction_id)


def _audit(owner_id: str, persona_id: str, interaction_id: str) -> Insert:
    """One neutral row: ids only, the same action for a release and a re-block."""
    return insert(audit_log_t).values(
        id=f"audit_{uuid.uuid4().hex}",
        user_id=owner_id,
        action=SETTING_CHANGED_ACTION,
        target=interaction_id,
        metadata={"persona_id": persona_id},
    )


class PgDisclosureStore:
    """Record and read the per-interaction disclosure fact (cloud and managed community)."""

    def __init__(self, engine: Engine) -> None:
        """Bind the owner-scoped (RLS) engine."""
        self._engine = engine

    def record(self, owner_id: str, persona_id: str, interaction_id: str) -> bool:
        """Hold that ``interaction_id`` carried a disclosure, and count it.

        Every call bumps ``disclosure_seq`` on an existing fact (D-ORCH-11-18-u), so a release
        given before it no longer covers the interaction: a new disclosure holds it back again.

        Returns whether the fact is held once this returns. ``False`` means the persona no
        longer exists for this owner (it was erased): nothing is written for it, and the caller
        must write nothing either. A database error other than that propagates, so the caller
        fails closed.

        The persona's row is read ``FOR KEY SHARE`` first, in the same transaction, so an
        erasure deleting it waits for this commit (and its cascade then takes the fact) or has
        already won (and nothing is written). Nothing here trusts ``rowcount``: psycopg reports
        -1 for an ``INSERT ... ON CONFLICT``, which once made a gone persona's fact look held
        (the PR #40 CI failure).
        """
        stmt = (
            pg_insert(wellbeing_disclosures)
            .values(owner_id=owner_id, persona_id=persona_id, interaction_id=interaction_id)
            .on_conflict_do_update(
                index_elements=[_T.owner_id, _T.interaction_id],
                set_={"disclosure_seq": _T.disclosure_seq + 1},
            )
        )
        try:
            with rls_connection(self._engine, owner_id) as conn:
                if conn.execute(_persona_lock(owner_id, persona_id)).first() is None:
                    _log.info(
                        "disclosure fact refused: its persona is gone persona_id={pid}",
                        pid=persona_id,
                    )
                    return False
                conn.execute(stmt)
                return True
        except IntegrityError:
            # Belt and braces: the key check refused the persona all the same.
            _log.info(
                "disclosure fact refused: its persona is gone persona_id={pid}", pid=persona_id
            )
            return False

    def disclosed(self, owner_id: str, refs: Iterable[str]) -> set[str]:
        """The refs among ``refs`` that ever carried a disclosure for this owner."""
        wanted = sorted(set(refs))
        if not wanted:
            return set()
        stmt = select(_T.interaction_id).where(
            _T.owner_id == owner_id, _T.interaction_id.in_(wanted)
        )
        with rls_connection(self._engine, owner_id) as conn:
            return {str(row[0]) for row in conn.execute(stmt).all()}

    def states(self, owner_id: str, refs: Iterable[str]) -> dict[str, DisclosureState]:
        """Each fact among ``refs``: its disclosure count and its release, if any."""
        wanted = sorted(set(refs))
        if not wanted:
            return {}
        stmt = select(
            _T.interaction_id, _T.disclosure_seq, _T.released_seq, _T.released_at, _T.created_at
        ).where(_T.owner_id == owner_id, _T.interaction_id.in_(wanted))
        with rls_connection(self._engine, owner_id) as conn:
            rows = conn.execute(stmt).all()
        return {
            str(row.interaction_id): DisclosureState(
                disclosure_seq=int(row.disclosure_seq),
                released_seq=None if row.released_seq is None else int(row.released_seq),
                released_at=row.released_at,
                created_at=row.created_at,
            )
            for row in rows
        }

    def release(
        self,
        owner_id: str,
        persona_id: str,
        interaction_id: str,
        *,
        version: int,
        at: datetime,
    ) -> ReleaseOutcome:
        """Release the disclosures the user saw (``version``: their count, 0 for no fact).

        One transaction: the persona row held, the fact row locked, the count compared, the
        release written and the audit row written. A count that moved since the user read it
        is a :attr:`ReleaseOutcome.CONFLICT` and writes nothing. With no fact (a tagged node
        alone holds the conversation, the merge case) the row is born released at one
        disclosure; a fact landing meanwhile is a conflict. A release that already covers the
        count is given again with a new ``released_at``: the caller (the control) only
        releases a conversation the user saw held.
        """
        try:
            with rls_connection(self._engine, owner_id) as conn:
                if conn.execute(_persona_lock(owner_id, persona_id)).first() is None:
                    return ReleaseOutcome.GONE
                locked = (
                    select(_T.disclosure_seq, _T.released_seq)
                    .where(*_fact(owner_id, interaction_id))
                    .with_for_update()
                )
                current = conn.execute(locked).first()
                seen = 0 if current is None else int(current.disclosure_seq)
                if seen != version:
                    return ReleaseOutcome.CONFLICT
                if current is None:
                    born = (
                        pg_insert(wellbeing_disclosures)
                        .values(
                            owner_id=owner_id,
                            persona_id=persona_id,
                            interaction_id=interaction_id,
                            disclosure_seq=1,
                            released_seq=1,
                            released_at=at,
                        )
                        .on_conflict_do_nothing()
                        .returning(_T.interaction_id)
                    )
                    if conn.execute(born).first() is None:
                        return ReleaseOutcome.CONFLICT
                else:
                    # A release already covering the count is given again and re-stamped
                    # (PR #44 review MEDIUM-1): the ``written_at`` net held the conversation
                    # after it, and the user has released what they now saw.
                    conn.execute(
                        update(wellbeing_disclosures)
                        .where(*_fact(owner_id, interaction_id))
                        .values(released_seq=_T.disclosure_seq, released_at=at)
                    )
                conn.execute(_audit(owner_id, persona_id, interaction_id))
        except IntegrityError:
            _log.info("release refused: its persona is gone persona_id={pid}", pid=persona_id)
            return ReleaseOutcome.GONE
        # PR #44 review LOW-5: the conversation id would mark a disclosure in the logs.
        _log.info(
            "conversation released owner_id={oid} persona_id={pid}", oid=owner_id, pid=persona_id
        )
        return ReleaseOutcome.CHANGED

    def hold(self, owner_id: str, persona_id: str, interaction_id: str) -> ReleaseOutcome:
        """Undo a release: the conversation is held back again. Audited like a release."""
        stmt = (
            update(wellbeing_disclosures)
            .where(*_fact(owner_id, interaction_id), _T.released_seq.isnot(None))
            .values(released_seq=None, released_at=None)
            .returning(_T.interaction_id)
        )
        with rls_connection(self._engine, owner_id) as conn:
            if conn.execute(stmt).first() is None:
                return ReleaseOutcome.UNCHANGED
            conn.execute(_audit(owner_id, persona_id, interaction_id))
        _log.info(
            "conversation held back again owner_id={oid} persona_id={pid}",
            oid=owner_id,
            pid=persona_id,
        )
        return ReleaseOutcome.CHANGED
