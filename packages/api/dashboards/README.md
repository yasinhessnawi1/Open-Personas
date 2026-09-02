# Open Persona observability dashboards

Three committed Grafana dashboards that read straight from the live `persona-api`
Postgres. System health was a gap for a while, because the earlier schema couldn't
capture the request telemetry it needed. That is closed.

| File | View | Data source |
|---|---|---|
| [`01_per_persona_usage.json`](01_per_persona_usage.json) | Per persona usage | `personas`, `conversations`, `turn_logs`, `memory_chunks` |
| [`02_routing_health.json`](02_routing_health.json) | Routing health | `turn_logs` |
| [`03_system_health.json`](03_system_health.json) | System health | `request_telemetry` |

## Setup: the read-only role

A plain `SELECT`-only role is **not enough**. The tenant tables `FORCE ROW LEVEL
SECURITY` and fail closed when `app.current_user_id` is unset, so a normal Grafana
connection sees **zero rows**. These dashboards are operator-only and have to read
across tenants, so provision a read-only role with `BYPASSRLS`:

```sql
-- run as the database superuser, ONCE per environment
CREATE ROLE grafana_ro LOGIN PASSWORD '<strong-password>' NOSUPERUSER BYPASSRLS;
GRANT USAGE ON SCHEMA public TO grafana_ro;
GRANT SELECT ON personas, conversations, turn_logs, memory_chunks,
                  credit_transactions, request_telemetry TO grafana_ro;
```

`request_telemetry` is a **non-RLS platform table**, so a plain `SELECT` grant
would do. It is granted here alongside the RLS tables so one operator role reads
every dashboard. It is operator forensic (no tenant data beyond the route
template, status, and latency), but never expose `grafana_ro` to a tenant facing
surface.

Do **not** hand this role to the web app or any tenant facing surface. It
intentionally bypasses RLS for operator visibility.

## Setup: the Grafana datasource

Add a Postgres datasource in Grafana pointing at the same DB the API uses
(`persona`), with `grafana_ro` as the user. Set the datasource **uid** to match
the dashboards' `${DS_POSTGRES}` placeholder, or edit the dashboards to your uid.

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

Upload each file in turn and bind it to the `persona-pg` datasource when
prompted. All of them are tagged `open-persona` for discovery.

## What's in each dashboard

### Per persona usage

- **Conversations per persona (30d)**: `conversations` GROUP BY `persona_id`.
- **Average turns per conversation (over time)**: derived from `turn_logs` (turns
  per `conversation_id`, day bucketed).
- **Episodic chunk count per persona**: `memory_chunks` WHERE `kind='episodic'`
  AND `superseded_by IS NULL` GROUP BY `persona_id`. This is the soak measurement
  hook for the eviction decision.
- **Compaction events per persona**: derived as `⌊compacted_up_to / 10⌋`
  (`compact_every=10`). An approximation, not an event counter.

### Routing health

- **Tier distribution (frontier / mid / small)**: stacked area from
  `turn_logs.tier_used`. Makes the architecture's tier routing thesis visible.
- **Cost per conversation**: `SUM(turn_logs.cost_cents) / 100.0` per conversation.
  An estimate, not billing.
- **Tool calls per turn**: a histogram of `turn_logs.tool_calls`.
- **Skill activations per day**: `COUNT(*) WHERE skill_used IS NOT NULL`, grouped
  by day and skill.

### System health

The endpoint level view, backed by the `request_telemetry` table and populated by
`RequestTelemetryMiddleware`: one row per HTTP request, carrying the matched
`route_template` (NOT the raw path), `method`, `status_code`, `duration_ms`, and
`timestamp`. The write is **buffered and flushed off the request path, fail
soft**, so a dropped telemetry row never breaks a request. Panels:

- **Requests per endpoint (count per minute)**: `COUNT(*)` GROUP BY
  `route_template`.
- **Error rate by status class (2xx / 4xx / 5xx)**: stacked, `status_code / 100`.
- **Rate limit rejections per minute**: `COUNT(*) WHERE status_code = 429`.
- **Latency per endpoint, p50 / p95 / p99**: `percentile_cont` over the windowed
  rows. The known percentile slowness only bites at 300 TB or on unbounded scans,
  not on persona traffic over a bounded `$__timeFilter` window. A t-digest or
  pre-bucketing is the escalation if telemetry row volume ever forces it.
- **Overall p95 / p99 latency (all endpoints)**: a trend line.

These aggregate **across all workers and instances**, because every process writes
to the one `request_telemetry` table. A per process Prometheus histogram would
need a scrape and merge server, which is exactly the new metrics platform this
approach avoids. **Provider and model availability stays in routing health**
(`turn_logs` already covers it). In-flight and saturation gauges plus circuit
breaker signals are the next additive step, not v1.

## Versioning

The dashboards' `version: 1` reflects the initial commit. Bump it when you edit
the JSON in-repo; re-importing in Grafana keeps the runtime version separate.
