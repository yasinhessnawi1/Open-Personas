"""The durable wellbeing disclosure fact, over ``wellbeing_disclosures`` (R9-253).

Synthesis and the direct write record that an interaction carried a wellbeing disclosure;
the initiative wellbeing gate reads it. The fact holds the owner, the persona, the
interaction id and a time, never the disclosed text or its category (D-ORCH-11-18-m), and no
node or conversation deletion clears it: only the persona's or the account's erasure does
(both foreign keys cascade).

Every statement runs in its own short owner-scoped transaction (``rls_connection``), so a
fact written ahead of a merge stays written whatever happens to the merge: the gate errs on
the side of holding back. Logs carry ids only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.logging import get_logger
from sqlalchemy import literal, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from persona_api.db.engine import rls_connection
from persona_api.db.models import personas as personas_t
from persona_api.db.models import wellbeing_disclosures

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlalchemy import Engine

__all__ = ["PgDisclosureStore"]

_log = get_logger("api.initiative.disclosures")


class PgDisclosureStore:
    """Record and read the per-interaction disclosure fact (cloud and managed community)."""

    def __init__(self, engine: Engine) -> None:
        """Bind the owner-scoped (RLS) engine."""
        self._engine = engine

    def record(self, owner_id: str, persona_id: str, interaction_id: str) -> bool:
        """Hold that ``interaction_id`` carried a disclosure. Idempotent.

        Returns whether the fact is held once this returns. ``False`` means the persona no
        longer exists for this owner (it was erased): nothing is written for it, and the caller
        must write nothing either. A database error other than that propagates, so the caller
        fails closed.
        """
        source = select(literal(owner_id), personas_t.c.id, literal(interaction_id)).where(
            personas_t.c.id == persona_id, personas_t.c.owner_id == owner_id
        )
        stmt = (
            pg_insert(wellbeing_disclosures)
            .from_select(["owner_id", "persona_id", "interaction_id"], source)
            .on_conflict_do_nothing()
        )
        held = select(wellbeing_disclosures.c.interaction_id).where(
            wellbeing_disclosures.c.owner_id == owner_id,
            wellbeing_disclosures.c.interaction_id == interaction_id,
        )
        try:
            with rls_connection(self._engine, owner_id) as conn:
                if conn.execute(stmt).rowcount:
                    return True
                return conn.execute(held).first() is not None
        except IntegrityError:
            # The persona's row went between the read above and the key check: it was erased.
            _log.info(
                "disclosure fact refused: its persona is gone persona_id={pid}", pid=persona_id
            )
            return False

    def disclosed(self, owner_id: str, refs: Iterable[str]) -> set[str]:
        """The refs among ``refs`` that ever carried a disclosure for this owner."""
        wanted = sorted(set(refs))
        if not wanted:
            return set()
        stmt = select(wellbeing_disclosures.c.interaction_id).where(
            wellbeing_disclosures.c.owner_id == owner_id,
            wellbeing_disclosures.c.interaction_id.in_(wanted),
        )
        with rls_connection(self._engine, owner_id) as conn:
            return {str(row[0]) for row in conn.execute(stmt).all()}
