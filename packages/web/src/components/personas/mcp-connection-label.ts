/**
 * N6 (Per-Tenant MCP) T7 — the not-connected signal, friendly-label mapping.
 *
 * Maps the backend connection signal (`GET /personas/{id}/mcp-connections`:
 * `{ server_name, connected, reason }`, the T1 vocabulary) onto a FRIENDLY,
 * plain-language badge — the R4 apps-grammar discipline: a non-dev must understand
 * "Starting…", "Needs attention", "Not connected" — the raw enum `reason` never
 * reaches the UI. Pure (no I/O, no React) so the whole vocabulary is render-testable.
 *
 * Closes the user-visible half of R4-C1-21: "assigned" is shown distinctly from
 * "working" — a connected app reads Connected; anything else names why in words.
 */

/** The backend reason vocabulary (mirrors the api `NotConnectedReason` Literal). */
export type ConnectionReason =
  | "starting"
  | "spawn_failed"
  | "stopped"
  | "fly_outage"
  | "no_key"
  | "unvetted"
  | "runtime_capacity"
  | "not_enabled";

/** One assigned server's connection status, as returned by the API. */
export interface McpConnectionStatus {
  readonly server_name: string;
  readonly connected: boolean;
  readonly reason: ConnectionReason | null;
}

/** The badge tone drives the color; `ok` = working, `pending` = transient, `warn` = attention. */
export type ConnectionTone = "ok" | "pending" | "warn";

export interface ConnectionBadge {
  /** The i18n key under `apps.connection.*` — NEVER the raw enum. */
  readonly labelKey: string;
  readonly tone: ConnectionTone;
}

/** reason → (friendly i18n key, tone). Every T1 reason is covered (no raw enum leaks). */
const REASON_BADGE: Record<ConnectionReason, ConnectionBadge> = {
  starting: { labelKey: "apps.connection.starting", tone: "pending" },
  stopped: { labelKey: "apps.connection.stopped", tone: "pending" },
  not_enabled: { labelKey: "apps.connection.notEnabled", tone: "pending" },
  spawn_failed: { labelKey: "apps.connection.spawnFailed", tone: "warn" },
  fly_outage: { labelKey: "apps.connection.flyOutage", tone: "warn" },
  no_key: { labelKey: "apps.connection.noKey", tone: "warn" },
  unvetted: { labelKey: "apps.connection.unvetted", tone: "warn" },
  runtime_capacity: {
    labelKey: "apps.connection.runtimeCapacity",
    tone: "warn",
  },
};

const CONNECTED_BADGE: ConnectionBadge = {
  labelKey: "apps.connection.connected",
  tone: "ok",
};

/**
 * Resolve a connection status to its friendly badge (i18n key + tone).
 *
 * A connected server → the `connected` badge; otherwise the reason's badge. A
 * malformed not-connected-without-reason (the api validator forbids it) falls back
 * to `not_enabled` so the UI never shows a blank or a raw enum.
 */
export function connectionBadge(status: {
  connected: boolean;
  reason: ConnectionReason | null;
}): ConnectionBadge {
  if (status.connected) return CONNECTED_BADGE;
  return (
    REASON_BADGE[status.reason ?? "not_enabled"] ?? REASON_BADGE.not_enabled
  );
}
