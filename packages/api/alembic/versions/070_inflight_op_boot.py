"""A long-op slot names the api instance and the process boot that took it (R9-318).

A chat turn abandoned at the shutdown deadline never reaches its ``finally``, so its
``inflight_ops`` slot is not released, and the TTL sweep frees it only after an hour. For that
hour a user at the concurrency cap gets 429. Only chat turns take a long-op slot: since W1
(D-W1-24) an agentic one-off runs on the worker under its per-user claim cap instead.

With these two columns the boot sweep (``persona.concurrency.release_previous_boot_long_ops``,
run by the api's restart sweep) frees every slot this instance's earlier boots left, at once,
and keeps the slots of the current process and of any other instance.

Both columns are nullable text with no default. A slot written by the old process while this
migration runs, or before it, has neither, and the sweep treats it as an earlier boot's: only an
earlier process can have written it. Idempotent split-home: on a fresh database ``001_initial``'s
``metadata.create_all`` already built them from the canonical model.

Locking (as 057, 058 and 069): ``lock_timeout`` is 5 s, so on a busy table the migration fails
fast and can be retried. Adding a nullable column with no default is a catalog-only change.

Downgrade drops both columns. Code before 070 does not write or read them.

Revision ID: 070_inflight_op_boot
Revises: 069_persona_create_complete
Create Date: 2026-11-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "070_inflight_op_boot"
down_revision = "069_persona_create_complete"
branch_labels = None
depends_on = None

_TABLE = "inflight_ops"
_COLUMNS = ("instance_id", "boot_id")


def _existing() -> set[str]:
    return {c["name"] for c in inspect(op.get_bind()).get_columns(_TABLE)}


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    existing = _existing()
    for name in _COLUMNS:
        if name not in existing:
            op.add_column(_TABLE, sa.Column(name, sa.Text(), nullable=True))
    op.execute("RESET lock_timeout")


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    existing = _existing()
    for name in reversed(_COLUMNS):
        if name in existing:
            op.drop_column(_TABLE, name)
    op.execute("RESET lock_timeout")
