"""The deferred-digest sink — the concrete :class:`DigestSink` (Spec A6, A6-D-10).

Captures the over-cap PROGRESS chatter that ``CadenceGate`` routes to ``DIGEST`` (the seam A3
defined + named A6 to implement). Two operations:

- :meth:`defer` — the ``DigestSink`` Protocol write: park one chatter line (owner-scoped, RLS).
- :meth:`consume_undelivered` — the morning build's read: **mark-delivered ATOMICALLY** in ONE
  statement (``UPDATE … SET delivered_at = :now WHERE delivered_at IS NULL RETURNING …``) and return
  the claimed rows. The claim and the read are the same statement, so there is **no read-then-mark
  window**: a concurrent build never double-delivers and never re-reads an already-claimed row.

Deferred chatter is a **secondary** digest input — the review's main sections never depend on it
(A6-D-2/10). So the atomic-claim trade (a claimed row is lost if the response then fails, since it's
already marked delivered) is the right call for this non-load-bearing chatter: never double-deliver.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — a runtime Pydantic field type
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict
from sqlalchemy import insert, update

from persona_api.db.engine import rls_connection
from persona_api.db.models import deferred_digest as deferred_digest_t

if TYPE_CHECKING:
    from sqlalchemy import Engine

__all__ = ["DeferredDigestItem", "DeferredDigestStore"]


class DeferredDigestItem(BaseModel):
    """One claimed chatter line the morning build folds in (a secondary digest input)."""

    model_config = ConfigDict(frozen=True)

    id: str
    persona_id: str
    content: str
    created_at: datetime
    deferred_reason: str


class DeferredDigestStore:
    """The concrete :class:`~persona_api.approvals.cadence.DigestSink` over ``deferred_digest``."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def defer(self, owner_id: str, persona_id: str, content: str, *, now: datetime) -> None:
        """Park one over-cap chatter line for the morning review (the ``DigestSink`` write)."""
        with rls_connection(self._engine, owner_id) as conn:
            conn.execute(
                insert(deferred_digest_t).values(
                    owner_id=owner_id,
                    persona_id=persona_id,
                    content=content,
                    deferred_reason="progress",
                    created_at=now,
                )
            )

    def consume_undelivered(self, owner_id: str, *, now: datetime) -> list[DeferredDigestItem]:
        """Atomically claim + return the owner's undelivered chatter, oldest-first.

        ONE statement marks every undelivered row delivered and returns it — no read-then-mark
        window (no double-deliver, no silent re-read). The caller builds the digest FROM the
        returned set (A6-D-10 binding); ordering is applied here (RETURNING order is unspecified).
        """
        with rls_connection(self._engine, owner_id) as conn:
            rows = (
                conn.execute(
                    update(deferred_digest_t)
                    .where(
                        deferred_digest_t.c.owner_id == owner_id,
                        deferred_digest_t.c.delivered_at.is_(None),
                    )
                    .values(delivered_at=now)
                    .returning(
                        deferred_digest_t.c.id,
                        deferred_digest_t.c.persona_id,
                        deferred_digest_t.c.content,
                        deferred_digest_t.c.created_at,
                        deferred_digest_t.c.deferred_reason,
                    )
                )
                .mappings()
                .all()
            )
        items = [
            DeferredDigestItem(
                id=str(r["id"]),
                persona_id=str(r["persona_id"]),
                content=str(r["content"]),
                created_at=r["created_at"],
                deferred_reason=str(r["deferred_reason"]),
            )
            for r in rows
        ]
        return sorted(items, key=lambda i: i.created_at)
