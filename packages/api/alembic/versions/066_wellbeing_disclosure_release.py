"""The "fine to bring this up" release on the disclosure fact (D-ORCH-11-18-u).

The owner approved a per-conversation control (D-OWNER-11-18 (16)): the user can say a
conversation that carried a wellbeing disclosure may be drawn on again by the persona's
unprompted initiative. The release lives on the fact row in ``wellbeing_disclosures``:

- ``disclosure_seq`` counts the disclosures recorded for the interaction (each record bumps it);
- ``released_seq`` is the count the user's release covers, so a disclosure recorded after the
  release (a higher count) holds the conversation back again;
- ``released_at`` is when the release was given.

Ids, counters and times only, like the fact itself. A row deployed under 065 gets one
disclosure and no release, so the upgrade releases nothing. The row keeps its forced RLS
policy and both cascades, so erasure needs nothing new.

Idempotent split-home: ``001_initial``'s ``metadata.create_all`` builds the columns and checks
on a fresh database, so the columns are added ``IF NOT EXISTS`` and each check only when no
constraint has its name.

Revision ID: 066_wellbeing_disclosure_release
Revises: 065_wellbeing_disclosures
Create Date: 2026-11-18
"""

from __future__ import annotations

from alembic import op

revision = "066_wellbeing_disclosure_release"
down_revision = "065_wellbeing_disclosures"
branch_labels = None
depends_on = None

_TABLE = "wellbeing_disclosures"

COLUMNS = (
    f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS disclosure_seq bigint NOT NULL DEFAULT 1",
    f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS released_seq bigint",
    f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS released_at timestamptz",
)

#: The model's checks, by name (a unit test pins them equal).
CHECKS = {
    "ck_wellbeing_disclosures_release_seq": (
        "released_seq IS NULL OR released_seq <= disclosure_seq"
    ),
    "ck_wellbeing_disclosures_release_pair": "(released_seq IS NULL) = (released_at IS NULL)",
}


def add_check_sql(name: str, expression: str) -> str:
    """Add the check unless a constraint already has its name (a fresh database has it)."""
    return (
        "DO $$ BEGIN "
        f"IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = '{name}') THEN "
        f"ALTER TABLE {_TABLE} ADD CONSTRAINT {name} CHECK ({expression}); "
        "END IF; END $$"
    )


def upgrade() -> None:
    for statement in COLUMNS:
        op.execute(statement)
    for name, expression in CHECKS.items():
        op.execute(add_check_sql(name, expression))


def downgrade() -> None:
    for name in CHECKS:
        op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {name}")
    for column in ("released_at", "released_seq", "disclosure_seq"):
        op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {column}")
