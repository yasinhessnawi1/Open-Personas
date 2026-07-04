import { describe, expect, it } from "vitest";
import type { MemoryLinkEdge, MemoryNodeSummary } from "@/lib/api";
import {
  capWindow,
  type GraphWindow,
  mergeWindow,
  protectedIds,
  removeFromWindow,
} from "./window-ops";

const node = (id: string): MemoryNodeSummary => ({
  id,
  kind: "concept",
  label: id,
  wellbeing_category: null,
  degree: 1,
});
const edge = (
  s: string,
  t: string,
  link_type: MemoryLinkEdge["link_type"] = "semantic",
): MemoryLinkEdge => ({
  src_node_id: s,
  dst_node_id: t,
  link_type,
  weight: null,
});

describe("mergeWindow", () => {
  it("appends new nodes/links and de-dupes existing ones", () => {
    const a: GraphWindow = {
      nodes: [node("x"), node("y")],
      links: [edge("x", "y")],
    };
    const b: GraphWindow = {
      nodes: [node("y"), node("z")], // y is a dup
      links: [edge("x", "y"), edge("y", "z")], // x->y is a dup
    };
    const merged = mergeWindow(a, b);
    expect(merged.nodes.map((n) => n.id)).toEqual(["x", "y", "z"]);
    expect(merged.links).toHaveLength(2);
  });

  it("distinguishes links of different types between the same pair", () => {
    const a: GraphWindow = {
      nodes: [node("x"), node("y")],
      links: [edge("x", "y", "semantic")],
    };
    const b: GraphWindow = { nodes: [], links: [edge("x", "y", "causal")] };
    expect(mergeWindow(a, b).links).toHaveLength(2);
  });
});

describe("removeFromWindow", () => {
  it("drops the node and every incident link (deletion)", () => {
    const w: GraphWindow = {
      nodes: [node("x"), node("y"), node("z")],
      links: [edge("x", "y"), edge("y", "z"), edge("x", "z")],
    };
    const after = removeFromWindow(w, "y");
    expect(after.nodes.map((n) => n.id)).toEqual(["x", "z"]);
    expect(after.links).toEqual([edge("x", "z")]); // only the x-z edge survives
  });
});

describe("protectedIds", () => {
  it("returns the focus + its neighbours, both directions", () => {
    const links = [edge("a", "b"), edge("c", "a"), edge("d", "e")];
    expect(protectedIds(links, "a")).toEqual(new Set(["a", "b", "c"]));
    expect(protectedIds(links, null).size).toBe(0);
  });
});

describe("capWindow", () => {
  it("is a no-op under the cap", () => {
    const w: GraphWindow = {
      nodes: [node("a"), node("b")],
      links: [edge("a", "b")],
    };
    expect(capWindow(w, new Set(["a"]), 5)).toBe(w);
  });

  it("keeps protected + most-recent, evicts oldest, prunes dangling links", () => {
    const w: GraphWindow = {
      nodes: ["a", "b", "c", "d", "e"].map(node), // oldest → newest
      links: [edge("a", "b"), edge("d", "e"), edge("c", "d")],
    };
    // cap 3, protect "a" (an old node) — should keep a + the two newest (d, e)
    const capped = capWindow(w, new Set(["a"]), 3);
    expect(capped.nodes.map((n) => n.id).sort()).toEqual(["a", "d", "e"]);
    // b and c are evicted → only the d-e link (both survivors) remains
    expect(capped.links).toEqual([edge("d", "e")]);
  });

  it("never drops the protected set, even when it exceeds the cap", () => {
    const w: GraphWindow = { nodes: ["a", "b", "c", "d"].map(node), links: [] };
    const capped = capWindow(w, new Set(["a", "b", "c"]), 2);
    // focus + neighbours are never evicted — d (unprotected) is dropped
    expect(capped.nodes.map((n) => n.id).sort()).toEqual(["a", "b", "c"]);
  });
});
