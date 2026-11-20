"""The durable wellbeing disclosure fact: ``wellbeing_disclosures`` + RLS + backfill (R9-253).

The initiative wellbeing gate derived "this interaction carried a disclosure" from the tagged
graph nodes that exist now, so a user forgetting the tagged node un-gated the untagged nodes
synthesis learned beside it. This table is the fact the gate reads instead: one row per
(owner, interaction) that ever carried a disclosure, written by synthesis and by the direct
write, never cleared by a node or conversation deletion. It holds ids and a time only, never
the text or the category (D-ORCH-11-18-m). It goes with its persona (FK cascade at the persona
erasure's row step) or its account (the ``users`` cascade).

Owner-scoped under RLS like every tenant table: ENABLE + FORCE + a ``user_isolation`` policy
with USING and WITH CHECK, created ENTIRELY here (the 015 template), deliberately NOT in
``persona_api.db.rls._POLICIES``. No GRANTs (D-07-5): ``release.py`` re-applies them on every
deploy. Idempotent split-home: ``Table.create(checkfirst=True)`` no-ops on a fresh DB, where
``001_initial``'s ``metadata.create_all`` already built it from the canonical model.

The backfill gives every interaction a tagged node cites today its fact, so a node forgotten
after this deploy no longer un-gates it. A disclosure whose tagged node was deleted before the
deploy cannot be recovered: nothing of it is left to read. The fact's persona is the persona
of the interaction's row (conversation, run or task) or, when that row is gone, the persona
the provenance entry names; a row is written only when that persona still exists for the
owner. The migration role bypasses RLS, so every owner's nodes are read.

Revision ID: 065_wellbeing_disclosures
Revises: 064_account_erasure
Create Date: 2026-11-18
"""

from __future__ import annotations

from alembic import op
from persona_api.db.models import wellbeing_disclosures

revision = "065_wellbeing_disclosures"
down_revision = "064_account_erasure"
branch_labels = None
depends_on = None

_TABLE = "wellbeing_disclosures"

# The missing-ok ``current_setting(..., true)`` form fails CLOSED: an unset GUC yields NULL,
# which matches no row.
_PREDICATE = "owner_id = current_setting('app.current_user_id', true)"

POLICY = f"CREATE POLICY user_isolation ON {_TABLE} USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"

#: Every interaction a tagged node cites today, with the persona it belongs to.
BACKFILL = f"""
INSERT INTO {_TABLE} (owner_id, persona_id, interaction_id)
SELECT DISTINCT ON (n.owner_id, e.interaction_id) n.owner_id, p.id, e.interaction_id
FROM graph_nodes n
CROSS JOIN LATERAL (
    SELECT entry ->> 'interaction_id' AS interaction_id, entry ->> 'persona_id' AS named
    FROM jsonb_array_elements(
        CASE WHEN jsonb_typeof(n.provenance) = 'array' THEN n.provenance ELSE '[]'::jsonb END
    ) AS entry
) e
LEFT JOIN conversations c ON c.id = e.interaction_id AND c.owner_id = n.owner_id
LEFT JOIN runs r ON r.id = e.interaction_id AND r.owner_id = n.owner_id
LEFT JOIN tasks t ON t.id = e.interaction_id AND t.owner_id = n.owner_id
JOIN personas p
    ON p.owner_id = n.owner_id
    AND p.id = COALESCE(c.persona_id, r.persona_id, t.persona_id, e.named)
WHERE n.wellbeing_category IS NOT NULL AND e.interaction_id IS NOT NULL
ORDER BY n.owner_id, e.interaction_id
ON CONFLICT DO NOTHING
"""


def upgrade() -> None:
    wellbeing_disclosures.create(op.get_bind(), checkfirst=True)
    op.execute(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS user_isolation ON {_TABLE}")
    op.execute(POLICY)
    op.execute(BACKFILL)


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS user_isolation ON {_TABLE}")
    wellbeing_disclosures.drop(op.get_bind(), checkfirst=True)
