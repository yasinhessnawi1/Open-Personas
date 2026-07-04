"""K5 Memory first-paint seed index — graph_nodes(owner_id, created_at).

Spec K5 (K5-D-8, B1-refined). The Memory view's no-focus seed query is
``WHERE owner_id = :o ORDER BY created_at DESC LIMIT :n``. Measured on a 50k-node
graph, the prior degree-aggregate seed was ~893ms (O(N+E), full Seq Scan + sort);
this composite index turns the seed into a bounded backward index-scan — sub-ms,
O(limit) regardless of graph size (`EXPLAIN`: Index Scan, no Sort/Seq Scan).

ADDITIVE + concurrency-safe shape: a plain B-tree index on existing columns, no
table rewrite, no data migration. Mirrors core's
``_schema.py::ix_graph_nodes_owner_created`` (the split-home views stay in step).

> Renumbered at merge-back: authored as 024 off ``023``; re-pointed to 035 off
> ``034_schedules_calendar`` (the real head when K5 landed). K8/A5 renumber after.

Revises: 034_schedules_calendar
"""

from __future__ import annotations

from alembic import op

revision = "035_memory_seed_index"
down_revision = "034_schedules_calendar"
branch_labels = None
depends_on = None

_INDEX = "ix_graph_nodes_owner_created"


def upgrade() -> None:
    op.create_index(
        _INDEX,
        "graph_nodes",
        ["owner_id", "created_at"],
        unique=False,
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name="graph_nodes", if_exists=True)
