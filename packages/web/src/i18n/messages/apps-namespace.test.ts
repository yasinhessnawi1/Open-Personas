/**
 * N3 (MCP-as-apps) Task 2 — the `apps.*` i18n namespace contract.
 *
 * These are the canonical strings Tasks 3–5 (app-state model, apps chooser,
 * persona-detail tombstone) reference. This test pins:
 *   1. the key STRUCTURE the components depend on (a missing key is caught here,
 *      not at render time);
 *   2. the HONESTY CONSTRAINTS that are fixed by the Phase-3 decisions and must
 *      not regress as wording refines:
 *      - no raw "MCP server" jargon in the primary apps surface (the kickoff's
 *        consistent-"apps"-copy requirement);
 *      - needs-setup says the app DECLARES a requirement + names WHO sets it
 *        ("deployment level"), never "your credential is missing" and never a
 *        user verb (N3-D-9/D-10);
 *      - unavailable = removed-after-enable, surfaced gracefully (N3-D-9).
 */

import { describe, expect, it } from "vitest";
import messages from "@/i18n/messages/en.json";

const apps = messages.apps;

/** Collect every string value in the `apps` namespace (recursively). */
function allStrings(node: unknown): string[] {
  if (typeof node === "string") return [node];
  if (node && typeof node === "object") {
    return Object.values(node).flatMap(allStrings);
  }
  return [];
}

describe("apps.* i18n namespace", () => {
  it("exposes the chooser + per-app framing keys Tasks 3–5 reference", () => {
    expect(apps.title).toBeTruthy();
    expect(apps.subtitle).toBeTruthy();
    expect(apps.searchPlaceholder).toBeTruthy();
    expect(apps.empty).toBeTruthy();
    expect(apps.searchEmpty).toContain("{query}");
    expect(apps.open).toContain("{name}");
    expect(apps.back).toBeTruthy();
    expect(apps.capability).toBeTruthy();
    expect(apps.toolsHeading).toBeTruthy();
  });

  it("names all four states", () => {
    expect(apps.state.available).toBeTruthy();
    expect(apps.state.needsSetup).toBeTruthy();
    expect(apps.state.enabled).toBeTruthy();
    expect(apps.state.unavailable).toBeTruthy();
  });

  it("exposes the enable/disable affordance copy", () => {
    expect(apps.enable.enable).toBeTruthy();
    expect(apps.enable.disable).toBeTruthy();
  });

  it("exposes the trust-label keys (N3-D-8 legible-not-opaque)", () => {
    for (const key of [
      "heading",
      "signed",
      "unsigned",
      "riskLabel",
      "image",
      "source",
      "sourceCommit",
      "allowHosts",
    ] as const) {
      expect(apps.trust[key]).toBeTruthy();
    }
    expect(apps.trust.riskLabel).toContain("{risk}");
    expect(apps.trust.allowHosts).toContain("{hosts}");
    expect(apps.trust.sourceCommit).toContain("{commit}");
  });

  it("R9-039: exposes the structured trust-row labels + the honest-empty-description fallback", () => {
    for (const key of ["runsLabel", "sourceLabel", "hostsLabel"] as const) {
      expect(apps.trust[key]).toBeTruthy();
    }
    expect(apps.detail.noDescription).toBeTruthy();
  });

  it("N3-D-7: the capability one-liner signals 'tools, once enabled' without faking a list", () => {
    const cap = apps.capability.toLowerCase();
    expect(cap).toContain("enabled");
    // No number / count baked into the line (the catalog carries no count — (c)).
    expect(apps.capability).not.toMatch(/\d/);
  });

  it("N3-D-10: needs-setup is an honest disclosure — declares + deployment-level, no user verb", () => {
    const ns = apps.needsSetup;
    // names WHO sets it
    expect(ns.managedNote.toLowerCase()).toContain("deployment level");
    expect(ns.credentialNeedsLabel.toLowerCase()).toContain("deployment level");
    expect(ns.credentialNeedsLabel).toContain("{env}");
    // the app DECLARES a requirement — NOT "your credential is missing"
    expect(ns.summary.toLowerCase()).toContain("declares");
    const nsBlob = allStrings(ns).join(" ").toLowerCase();
    expect(nsBlob).not.toContain("your credential is missing");
    expect(nsBlob).not.toContain("missing credential");
    // no user verb that implies an action N3 can't fulfil (no save/connect/add)
    expect(nsBlob).not.toMatch(
      /\b(connect|add token|set up your|enter your)\b/,
    );
  });

  it("N4 (Group D): the setup-form keys exist with their placeholders + an honest isolation line", () => {
    const sf = apps.setupForm;
    for (const key of [
      "intro",
      "credentialLabel",
      "credentialPlaceholder",
      "submit",
      "submitting",
      "done",
      "error",
      "errorAlready",
      "errorNotVetted",
    ] as const) {
      expect(sf[key]).toBeTruthy();
    }
    expect(sf.credentialLabel).toContain("{env}");
    expect(sf.done).toContain("{name}");
    // the isolation promise is stated honestly: the persona never sees the secret.
    expect(sf.intro.toLowerCase()).toContain("never sees");
  });

  it("N3-D-9: unavailable is removed-after-enable, surfaced gracefully", () => {
    const un = apps.unavailable;
    expect(un.tombstone).toContain("{name}");
    expect(un.tombstone.toLowerCase()).toContain("removed");
    expect(un.summary.toLowerCase()).toContain("removed");
  });

  it("uses 'apps' as the UX language — no raw 'MCP server' jargon in the namespace", () => {
    const blob = allStrings(apps).join("\n").toLowerCase();
    expect(blob).not.toContain("mcp server");
    expect(blob).not.toContain("mcp:");
  });
});
