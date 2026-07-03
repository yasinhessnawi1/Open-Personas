"""Per-request telemetry: ``request_telemetry`` table (Spec R5, R5-D-3).

Fills the D-11-5 / ARCHITECTURE §6.3 "system health" gap. ``turn_logs`` records
per-TURN model metrics but has no per-ENDPOINT / HTTP-status / error dimension,
and its ``latency_ms`` is model latency, not per-endpoint p99. This table is one
row per HTTP request, written by ``RequestTelemetryMiddleware`` through a buffered
background flush (never synchronously on the request path — R5-D-3), and read by
the §6.3 Grafana dashboard via the existing ``grafana_ro BYPASSRLS`` role.

NON-RLS platform table (operator forensics), plain engine, like ``turn_logs`` /
``audit_log``. ``timestamp`` is the request-completion moment captured in-band;
``route_template`` is the matched route pattern (bounded cardinality), never the
raw path. Indexes: BRIN on the append-only, time-ascending ``timestamp`` (the
windowed ``percentile_cont`` / group-by scans) + BTREE on ``route_template`` (the
per-endpoint GROUP BY).

**Grants.** No GRANTs here (D-07-5). ``persona_app`` gets blanket DML from prod
provisioning + the test fixture; the ``grafana_ro`` SELECT grant is a deploy-note
recorded alongside the dashboard JSON (``dashboards/README.md``) — the same
posture as the other observability tables.

Follows the 019/026 template: created from the canonical ``MetaData`` with
``checkfirst=True`` (fresh-DB ``001`` create_all already built it — harmless
no-op; a previously-deployed Postgres genuinely creates it). No RLS.

**Migration-slot coordination (R-19-1):** authored on the R5 placeholder chain
(``030_audit_events_postgres`` → this). Both R5 migrations' numbers +
``down_revision`` are RECOMPUTED against main's REAL head at merge-back (main head
has since advanced past 025) — do NOT rely on ``026`` / ``027`` surviving verbatim.

Revision ID: 031_request_telemetry
Revises: 030_audit_events_postgres
Create Date: 2026-07-02
"""

from __future__ import annotations

from alembic import op
from persona_api.db.models import request_telemetry

# PLACEHOLDER down_revision (R-19-1) — chained on R5's own 026; recomputed against
# main's real alembic head at merge-back. See the module docstring.
revision = "031_request_telemetry"
down_revision = "030_audit_events_postgres"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    request_telemetry.create(bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    request_telemetry.drop(bind, checkfirst=True)
