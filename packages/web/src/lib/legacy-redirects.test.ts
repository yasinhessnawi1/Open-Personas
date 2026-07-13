import { describe, expect, it } from "vitest";

import { LEGACY_REDIRECTS } from "./legacy-redirects";

describe("LEGACY_REDIRECTS (R11-B1)", () => {
  const bySource = Object.fromEntries(
    LEGACY_REDIRECTS.map((r) => [r.source, r.destination]),
  );

  it("re-homes every retired Activity route into /activity", () => {
    expect(bySource["/review"]).toBe("/activity");
    expect(bySource["/tasks"]).toBe("/activity/tasks");
    expect(bySource["/approvals"]).toBe("/activity/approvals");
  });

  it("carries the task-detail dynamic param through", () => {
    expect(bySource["/tasks/:taskId"]).toBe("/activity/tasks/:taskId");
  });

  it("leaves /settings/connectors alone — the D-R11-3 promotion was owner-reversed", () => {
    expect(bySource["/settings/connectors"]).toBeUndefined();
  });

  it("uses temporary redirects only — 308s outlive an IA still in motion", () => {
    for (const r of LEGACY_REDIRECTS) expect(r.permanent).toBe(false);
  });

  it("never redirects a source onto itself or another source", () => {
    const sources = new Set<string>(LEGACY_REDIRECTS.map((r) => r.source));
    for (const r of LEGACY_REDIRECTS) {
      expect(r.destination).not.toBe(r.source);
      expect(sources.has(r.destination)).toBe(false);
    }
  });
});
