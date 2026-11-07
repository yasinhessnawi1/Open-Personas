"""Deleting a persona clears only a channel's pointer to it, not the channel's owner.

``connector_channels`` points at its foregrounded persona through the composite foreign key
``fk_connector_channels_active_persona``: ``(active_persona_id, owner_id) -> personas(id,
owner_id)``, so a channel can never point at another tenant's persona (017). It was declared
``ON DELETE SET NULL``, and a plain ``SET NULL`` on a composite key nulls EVERY column in it.
``owner_id`` is NOT NULL, so deleting any persona that was some channel's active persona was
refused (found in the review of the persona id hotfix, 2026-11-07). The persona delete route
now clears the pointer itself before the row goes; this makes every other path that reaches
``personas`` (an account cascade through ``users``, an operator's SQL) behave the same.

``ON DELETE SET NULL (active_persona_id)`` names the one column to null. PostgreSQL added the
column list in version 15. Every deployment this ships to runs 16 (the hosted compose file and
the community's embedded server); on an older server, which only a self-hosted external
``DATABASE_URL`` could be, the constraint is left as it was and the route's own clearing is
what keeps a persona delete working there. The canonical model keeps the plain form, because
SQLite (the legacy community edition) has no column list and builds from the same metadata.

Revision ID: 060_channel_persona_set_null
Revises: 059_memory_chain_head_unique
Create Date: 2026-11-07
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "060_channel_persona_set_null"
down_revision = "059_memory_chain_head_unique"
branch_labels = None
depends_on = None

_FK = "fk_connector_channels_active_persona"
_REFERENCE = (
    f"ALTER TABLE connector_channels ADD CONSTRAINT {_FK} "
    "FOREIGN KEY (active_persona_id, owner_id) REFERENCES personas (id, owner_id) "
)
#: The first PostgreSQL version with a column list on ``ON DELETE SET NULL``.
_COLUMN_LIST_SINCE = 150000


def _supports_column_list() -> bool:
    version = op.get_bind().execute(text("SHOW server_version_num")).scalar_one()
    return int(version) >= _COLUMN_LIST_SINCE


def upgrade() -> None:
    if not _supports_column_list():
        return
    op.execute(f"ALTER TABLE connector_channels DROP CONSTRAINT IF EXISTS {_FK}")
    op.execute(_REFERENCE + "ON DELETE SET NULL (active_persona_id)")


def downgrade() -> None:
    if not _supports_column_list():
        return
    op.execute(f"ALTER TABLE connector_channels DROP CONSTRAINT IF EXISTS {_FK}")
    op.execute(_REFERENCE + "ON DELETE SET NULL")
