"""Multi-worker-safe audit: ``store_audit_events`` + ``tool_audit_events`` (Spec R5, R5-D-1).

R5 lifts the S08-4 single-uvicorn-worker assumption for the two JSONL audit logs.
Both ``persona.audit.JSONLAuditLogger`` (store mutations) and
``persona.tools.audit.JSONLToolAuditLogger`` (tool events) assume ONE writer — a
process-local ``threading.Lock`` that provides no cross-process guarantee — so
the moment the API runs on N workers / N Fly Machines they lose or interleave
entries. This migration adds the two Postgres tables the new
``PostgresAuditLogger`` / ``PostgresToolAuditLogger`` write to instead
(env-gated at ``_build_audit_logger``; default stays JSONL so community /
single-node is byte-unchanged).

PostgreSQL MVCC + WAL make concurrent INSERTs from N processes/connections
non-blocking, per-statement atomic, and durable — no lost / duplicated /
interleaved-corrupted rows and no explicit locking for an append-only pattern.
This is the D-19-5 "in-process state → shared store" pattern (mirrors
``rate_limit_buckets`` / ``PostgresRateLimitStore``), reused, not reinvented.

Both are **NON-RLS platform tables** (like ``audit_log`` / ``turn_logs`` /
``rate_limit_buckets``): written via a plain ``engine.begin()`` as
``persona_app``, no owner GUC, no RLS policy. Columns mirror the frozen Pydantic
events verbatim (``list``/``dict`` → JSON(B)); ``id`` / ``created_at`` are
server-defaulted (``_uuid_pk`` / ``now()``). Indexes: **BRIN on ``created_at``**
(append-only, timestamp-ascending → hundreds of × smaller than BTREE, faster
range scans for the ``since=`` read path) + BTREE on ``persona_id`` for the
equality read path.

**Grants.** No GRANTs here (D-07-5 — roles are provisioned out-of-band). The
integration ``migrated_engine`` fixture blanket-grants ``persona_app`` on ALL
TABLES, and prod provisioning does likewise; the prod tightening (SELECT+INSERT
on ``store_audit_events``, INSERT-only on ``tool_audit_events``) is a deploy-note
concern (Group D), matching the ``audit_log`` INSERT-only precedent.

Follows the 012/014/019 template: tables created from the canonical ``MetaData``
with ``checkfirst=True`` (idempotent — a fresh-install ``001`` ``create_all``
already made them, since they are now in ``persona_api.db.models``, so on a fresh
DB — incl. community SQLite — this is a harmless no-op; on a previously-deployed
Postgres it genuinely creates them). No RLS (non-RLS platform tables), so nothing
is added to ``persona_api.db.rls._POLICIES``.

**Migration-slot coordination (R-19-1):** authored against placeholder head
``025_avatar_source_provenance`` (main's Phase-3 head). Other in-flight R-/C-/N-/
P-track specs may land migrations before R5 merges, so this number +
``down_revision`` are RECOMPUTED against main's REAL head at merge-back — do NOT
rely on ``026`` / ``025`` surviving verbatim.

Revision ID: 030_audit_events_postgres
Revises: 029_users_identity_names
Create Date: 2026-07-02
"""

from __future__ import annotations

from alembic import op
from persona_api.db.models import store_audit_events, tool_audit_events

# PLACEHOLDER down_revision (R-19-1 chain numbering) — recomputed at merge-back
# against main's real alembic head. See the module docstring.
revision = "030_audit_events_postgres"
down_revision = "029_users_identity_names"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    # checkfirst=True: fresh-DB ``001`` create_all already built these (they are in
    # the canonical MetaData now), so this no-ops there; on a DB migrated before R5
    # it genuinely creates them. No RLS — these are non-RLS platform tables.
    store_audit_events.create(bind, checkfirst=True)
    tool_audit_events.create(bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    tool_audit_events.drop(bind, checkfirst=True)
    store_audit_events.drop(bind, checkfirst=True)
