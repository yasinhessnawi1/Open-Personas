import { describe, expect, it } from "vitest";
import {
  kindColor,
  type LinkType,
  linkEncoding,
  linkRelationKey,
  linkSortRank,
  type NodeKind,
  nodeRadius,
} from "./encoding";

const ALL_KINDS: NodeKind[] = [
  "concept",
  "fact",
  "preference",
  "trait",
  "goal",
  "circumstance",
  "entity",
];
const ALL_LINKS: LinkType[] = ["semantic", "entity", "temporal", "causal"];

describe("kindColor", () => {
  it("maps every K0 NodeKind to a distinct oklch colour", () => {
    const colours = ALL_KINDS.map(kindColor);
    expect(new Set(colours).size).toBe(ALL_KINDS.length);
    for (const c of colours) expect(c).toMatch(/^oklch\(/);
  });

  it("entity is visually distinct from the calm ramp (K5-D-3)", () => {
    expect(kindColor("concept")).not.toBe(kindColor("entity"));
  });

  it("falls back calmly to the concept tone for an unknown (future) kind", () => {
    expect(kindColor("quasar")).toBe(kindColor("concept"));
  });
});

describe("linkEncoding", () => {
  it("covers all four typed links with a distinct recipe", () => {
    for (const t of ALL_LINKS)
      expect(linkEncoding(t).color).toMatch(/^oklch\(/);
    // semantic is the quiet one; causal the loud, arrowed one.
    expect(linkEncoding("semantic").alpha).toBeLessThan(
      linkEncoding("causal").alpha,
    );
    expect(linkEncoding("causal").arrow).toBe(true);
    expect(linkEncoding("semantic").arrow).toBe(false);
    expect(linkEncoding("temporal").dash.length).toBeGreaterThan(0);
    expect(linkEncoding("entity").dash.length).toBe(0);
  });

  it("falls back to the quiet semantic recipe for an unknown link type", () => {
    expect(linkEncoding("teleological")).toEqual(linkEncoding("semantic"));
  });
});

describe("nodeRadius", () => {
  it("grows with degree but is clamped 5–19", () => {
    expect(nodeRadius(0)).toBe(5);
    expect(nodeRadius(-3)).toBe(5); // negative guarded
    expect(nodeRadius(100)).toBe(19);
    expect(nodeRadius(5)).toBeGreaterThan(nodeRadius(2));
  });
});

describe("link ordering + relation keys", () => {
  it("ranks structure before quiet semantics (causal < entity < temporal < semantic)", () => {
    expect(linkSortRank("causal")).toBeLessThan(linkSortRank("entity"));
    expect(linkSortRank("entity")).toBeLessThan(linkSortRank("temporal"));
    expect(linkSortRank("temporal")).toBeLessThan(linkSortRank("semantic"));
    expect(linkSortRank("unknown")).toBe(99);
  });

  it("maps each link type to a safe relation key", () => {
    for (const t of ALL_LINKS) expect(linkRelationKey(t)).toBe(t);
    expect(linkRelationKey("mystery")).toBe("semantic");
  });
});
