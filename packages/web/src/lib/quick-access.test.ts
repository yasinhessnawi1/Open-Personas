import { describe, expect, it } from "vitest";

import { rankPersonasByRecentUse } from "./quick-access";

const p = (id: string, created_at: string) => ({ id, created_at });
const c = (persona_id: string) => ({ persona_id });

describe("rankPersonasByRecentUse (R11-B2, D-R11-5)", () => {
  it("ranks by first appearance in the newest-first conversation list", () => {
    const personas = [
      p("a", "2026-01-01"),
      p("b", "2026-01-02"),
      p("c", "2026-01-03"),
    ];
    const convs = [c("b"), c("a"), c("b")];
    expect(
      rankPersonasByRecentUse(personas, convs, 4).map((x) => x.id),
    ).toEqual([
      "b",
      "a",
      "c", // never used → fills the tail
    ]);
  });

  it("fills never-used personas most-recently-created first", () => {
    const personas = [p("old", "2026-01-01"), p("new", "2026-06-01")];
    expect(rankPersonasByRecentUse(personas, [], 4).map((x) => x.id)).toEqual([
      "new",
      "old",
    ]);
  });

  it("caps at the limit and ignores conversations for unknown personas", () => {
    const personas = [p("a", "2026-01-01"), p("b", "2026-01-02")];
    const convs = [c("ghost"), c("a"), c("b")];
    expect(
      rankPersonasByRecentUse(personas, convs, 1).map((x) => x.id),
    ).toEqual(["a"]);
  });
});
