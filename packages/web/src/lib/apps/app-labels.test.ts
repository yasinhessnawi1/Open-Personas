/**
 * R4 T5 — the apps label-map contract.
 *
 * Mirrors the R4-C1-1 gallery-label lesson: a capability id that gains no
 * friendly label renders a raw id (or a MISSING_MESSAGE fallback) in the UI.
 * This pins the contract — every built-in tool + skill id resolves a real
 * `apps.catalog.<id>.{label,description}` — so adding an id without a label
 * (or a Python-side catalog addition mirrored here) fails LOUDLY, not silently.
 */
import { describe, expect, it } from "vitest";
import messages from "@/i18n/messages/en.json";
import { BUILTIN_APP_IDS, humanizeAppId, presentApp } from "./app-labels";

const catalog = (
  messages as {
    apps: {
      catalog: Record<string, { label?: string; description?: string }>;
    };
  }
).apps.catalog;

describe("apps label map (R4 T5)", () => {
  it("every built-in tool + skill id has a non-empty label + description", () => {
    for (const id of BUILTIN_APP_IDS) {
      const entry = catalog[id];
      expect(entry, `apps.catalog.${id} is missing from en.json`).toBeTruthy();
      expect(entry?.label, `apps.catalog.${id}.label is empty`).toBeTruthy();
      expect(
        entry?.description,
        `apps.catalog.${id}.description is empty`,
      ).toBeTruthy();
    }
  });

  it("declares no MORE labels than the canonical id set (stale-label guard)", () => {
    // Every catalog key must be a known built-in id — a leftover label for a
    // removed capability is dead weight and a sign the map drifted.
    for (const id of Object.keys(catalog)) {
      expect(
        BUILTIN_APP_IDS.includes(id),
        `apps.catalog.${id} has no matching built-in id`,
      ).toBe(true);
    }
  });
});

describe("presentApp (R4 T5)", () => {
  // A translator scoped to the `apps` namespace, backed by the real en.json.
  const t = Object.assign(
    (key: string) => {
      const path = key.split(".");
      let node: unknown = messages.apps;
      for (const part of path) {
        node = (node as Record<string, unknown>)?.[part];
      }
      return typeof node === "string" ? node : key;
    },
    {
      has: (key: string) => {
        const path = key.split(".");
        let node: unknown = messages.apps;
        for (const part of path) {
          node = (node as Record<string, unknown>)?.[part];
        }
        return typeof node === "string";
      },
    },
  );

  it("resolves a built-in tool id to its friendly label + description", () => {
    const app = presentApp("datetime", t);
    expect(app.label).toBe("Date & time");
    expect(app.description).toBe("Checks today's date and the current time.");
  });

  it("labels an MCP id from the catalog display_name", () => {
    const app = presentApp("mcp:google-flights", t, (name) =>
      name === "google-flights"
        ? { displayName: "Google Flights", description: "Search flights." }
        : undefined,
    );
    expect(app.label).toBe("Google Flights");
    expect(app.description).toBe("Search flights.");
  });

  it("humanises a built-in MCP server with no display_name (never a raw id)", () => {
    const app = presentApp("mcp:time", t, () => ({ displayName: "" }));
    expect(app.label).toBe("Time");
  });

  it("humanises an unknown id rather than leaking the raw id", () => {
    const app = presentApp("some_new_tool", t);
    expect(app.label).toBe("Some New Tool");
    expect(app.description).toBeNull();
  });

  it("humanizeAppId title-cases snake and kebab ids", () => {
    expect(humanizeAppId("google-flights")).toBe("Google Flights");
    expect(humanizeAppId("text_summarize")).toBe("Text Summarize");
    expect(humanizeAppId("mcp:google-flights")).toBe("Google Flights");
  });
});
