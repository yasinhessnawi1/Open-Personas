/**
 * N6 (Per-Tenant MCP) T7 — the friendly not-connected label mapping.
 *
 * Proves the R4 apps-grammar discipline: every T1 reason maps to a friendly
 * `apps.connection.*` key (never a raw enum), connected → the ok badge, and the
 * malformed-missing-reason case degrades safely rather than blanking.
 */

import { describe, expect, it } from "vitest";
import enMessages from "@/i18n/messages/en.json";
import { type ConnectionReason, connectionBadge } from "./mcp-connection-label";

const ALL_REASONS: ConnectionReason[] = [
  "starting",
  "spawn_failed",
  "stopped",
  "fly_outage",
  "no_key",
  "unvetted",
  "runtime_capacity",
  "not_enabled",
];

/** Resolve an `apps.connection.x` dotted key against the real en.json bundle. */
function resolve(key: string): string | undefined {
  return key
    .split(".")
    .reduce<unknown>(
      (acc, part) =>
        acc && typeof acc === "object"
          ? (acc as Record<string, unknown>)[part]
          : undefined,
      enMessages,
    ) as string | undefined;
}

describe("connectionBadge", () => {
  it("maps connected to the ok badge", () => {
    const b = connectionBadge({ connected: true, reason: null });
    expect(b.tone).toBe("ok");
    expect(b.labelKey).toBe("apps.connection.connected");
  });

  it.each(ALL_REASONS)(
    "maps reason %s to a friendly key that exists in en.json (never a raw enum)",
    (reason) => {
      const b = connectionBadge({ connected: false, reason });
      // The UI renders a translation KEY under apps.connection.*, never a raw enum.
      expect(b.labelKey).toMatch(/^apps\.connection\./);
      // The user-facing LABEL is friendly plain language — never the raw enum string
      // (no snake_case leaking through to the badge).
      const label = resolve(b.labelKey);
      expect(typeof label).toBe("string");
      expect(label).not.toBe(reason);
      expect(label).not.toContain("_");
    },
  );

  it("degrades a malformed missing-reason to not-connected, never blank", () => {
    const b = connectionBadge({ connected: false, reason: null });
    expect(b.labelKey).toBe("apps.connection.notEnabled");
    expect(resolve(b.labelKey)).toBeTruthy();
  });

  it("assigns warn tone to the attention states", () => {
    for (const r of [
      "spawn_failed",
      "fly_outage",
      "no_key",
      "unvetted",
      "runtime_capacity",
    ] as ConnectionReason[]) {
      expect(connectionBadge({ connected: false, reason: r }).tone).toBe(
        "warn",
      );
    }
  });
});
