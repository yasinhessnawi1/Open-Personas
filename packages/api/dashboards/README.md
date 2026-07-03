# Open Persona — Observability Dashboards (Spec 11 §6)

Three committed Grafana dashboards, reading from the live `persona-api`
Postgres. §6.3 (system health) was the D-11-5 gap; **Spec R5 (R5-D-3) closed it**
by adding the request-telemetry the earlier schema couldn't capture.

| File | Spec | Data source |
|---|---|---|
| [`01_per_persona_usage.json`](01_per_persona_usage.json) | §6.1 — per-persona usage | `personas`, `conversations`, `turn_logs`, `memory_chunks` |
| [`02_routing_health.json`](02_routing_health.json) | §6.2 — routing health | `turn_logs` |
| [`03_system_health.json`](03_system_health.json) | §6.3 — system health | `request_telemetry` |

## Setup — the read-only role (D-11-5)

A plain `SELECT`-only role is **not enough**: the tenant tables `FORCE ROW
LEVEL SECURITY` (D-07-5 / D-08-1) and fail closed when `app.current_user_id` is
unset, so a normal Grafana connection sees **zero rows**. The ops dashboards
are operator-only and must read across tenants — provision a read-only role
with `BYPASSRLS`:

```sql
-- run as the database superuser, ONCE per environment
CREATE ROLE grafana_ro LOGIN PASSWORD '<strong-password>' NOSUPERUSER BYPASSRLS;
GRANT USAGE ON SCHEMA public TO grafana_ro;
GRANT SELECT ON personas, conversations, turn_logs, memory_chunks,
                  credit_transactions, request_telemetry TO grafana_ro;
```

`request_telemetry` (Spec R5, R5-D-3) is a **non-RLS platform table**, so a plain
`SELECT` grant suffices — but it is granted here alongside the RLS tables so the
one operator role reads every dashboard. It is operator-forensic (no tenant data
beyond the route template + status + latency); still, never expose `grafana_ro`
to a tenant-facing surface.

Do **not** expose this role to the web app or any tenant-facing surface — it
intentionally bypasses RLS for operator visibility.

## Setup — Grafana datasource

Add a Postgres datasource in Grafana pointing at the same DB the API uses
(`persona`), with `grafana_ro` as the user. Set the datasource **uid** to
match the dashboards' `${DS_POSTGRES}` placeholder (or edit the dashboards to
your uid).

```text
Name:       persona-pg
Host:       <api-host>:5432   (or :5436 locally)
Database:   persona
User:       grafana_ro
SSL mode:   require            (in production)
```

## Importing the dashboards

```text
Grafana → Dashboards → New → Import → Upload JSON file
```

Upload each file in turn; bind it to the `persona-pg` datasource when prompted.
Both dashboards are tagged `open-persona` for discovery.

## What's in each dashboard

### §6.1 — Per-persona usage

- **Conversations per persona (30d)** — `conversations` GROUP BY `persona_id`.
- **Average turns per conversation (over time)** — derived from `turn_logs`
  (turns per `conversation_id`, day-bucketed).
- **Episodic chunk count per persona** — `memory_chunks` WHERE `kind='episodic'`
  AND `superseded_by IS NULL` GROUP BY `persona_id`. Spec-11 soak measurement
  hook for the eviction decision (D-11-4).
- **Compaction events per persona** — derived as `⌊compacted_up_to / 10⌋`
  (`compact_every=10`, spec 05). Approximation, not an event counter.

### §6.2 — Routing health

- **Tier distribution (frontier/mid/small)** — stacked-area from
  `turn_logs.tier_used`. Makes the architecture's tier-routing thesis visible.
- **Cost per conversation** — `SUM(turn_logs.cost_cents) / 100.0` per
  conversation. Estimate; not billing.
- **Tool calls per turn** — histogram of `turn_logs.tool_calls`.
- **Skill activations per day** — `COUNT(*) WHERE skill_used IS NOT NULL`,
  grouped by day + skill.

### §6.3 — System health (Spec R5, R5-D-3)

The endpoint-level view D-11-5 deferred. Backed by the new `request_telemetry`
table, populated by `RequestTelemetryMiddleware` (one row per HTTP request:
matched `route_template` — NOT the raw path — `method`, `status_code`,
`duration_ms`, `timestamp`). The write is **buffered + flushed off the request
path, fail-soft** — a dropped telemetry row never breaks a request. Panels:

- **Requests per endpoint (count / minute)** — `COUNT(*)` GROUP BY `route_template`.
- **Error rate by status class (2xx / 4xx / 5xx)** — stacked, `status_code / 100`.
- **Rate-limit rejections per minute** — `COUNT(*) WHERE status_code = 429`.
- **Latency per endpoint p50 / p95 / p99** — `percentile_cont` over the windowed
  rows (the known percentile-slowness only bites at 300 TB / unbounded scans, not
  persona traffic over a bounded `$__timeFilter` window). **t-digest /
  pre-bucketing is the v0.2 escalation** if telemetry row volume forces it.
- **Overall p95 / p99 latency (all endpoints)** — trend line.

Aggregates **across all workers / instances** because every process writes to the
one `request_telemetry` table — unlike a per-process Prometheus histogram, which
would need a scrape/merge server (the "new metrics platform" R5 deliberately
avoids). **Provider/model availability stays in §6.2** (`turn_logs` already covers
it); in-flight/saturation gauges + circuit-breaker signals are the next additive
step, not v1.

## Versioning

The dashboards' `version: 1` reflects the initial commit. Bump when editing
the JSON in-repo; re-importing in Grafana keeps the runtime version separate.
