"use client";

import Graph from "graphology";
import louvain from "graphology-communities-louvain";
import forceAtlas2 from "graphology-layout-forceatlas2";
import FA2LayoutSupervisor from "graphology-layout-forceatlas2/worker";
import { useEffect, useMemo, useRef } from "react";
import type { MemoryLinkEdge, MemoryNodeSummary } from "@/lib/api";

/**
 * Spec K5 — the off-main-thread force layout (K5-D-1).
 *
 * Builds a graphology `Graph` from the loaded window and runs ForceAtlas2 in a
 * real Web Worker via `FA2LayoutSupervisor` (the answer to `Memory.html`'s O(n²)
 * main-thread `tick`): the supervisor mutates each node's `x`/`y` attributes live
 * off the main thread; the canvas reads them every frame. Barnes-Hut kicks in for
 * larger windows. `graphology-communities-louvain` assigns stable `community` ids
 * for the far-zoom region LOD (K5-R-2).
 *
 * - `prefers-reduced-motion` ⇒ settle synchronously to a calm static layout, never
 *   a perpetually-moving worker (research §8).
 * - The supervisor is **killed on unmount / rebuild** so no Web Worker leaks across
 *   route changes; positions are cached first so a focus-expansion (window change)
 *   keeps settled nodes in place instead of reshuffling the whole map.
 */
const SETTLE_MS = 2600;
const BARNES_HUT_THRESHOLD = 250;

export function useForceLayout(
  nodes: readonly MemoryNodeSummary[],
  links: readonly MemoryLinkEdge[],
  reducedMotion: boolean,
): Graph {
  // Survives rebuilds: settled positions keyed by node id, so expanding the
  // window doesn't fling already-placed nodes to new random seeds.
  const positions = useRef<Map<string, { x: number; y: number }>>(new Map());

  const graph = useMemo(() => {
    const g = new Graph({
      type: "undirected",
      multi: false,
      allowSelfLoops: false,
    });
    const count = Math.max(1, nodes.length);
    nodes.forEach((node, i) => {
      const prev = positions.current.get(node.id);
      const angle = (i / count) * Math.PI * 2;
      g.addNode(node.id, {
        label: node.label,
        kind: node.kind,
        degree: node.degree,
        sensitive: Boolean(node.wellbeing_category),
        x: prev?.x ?? Math.cos(angle) * 220 + (Math.random() - 0.5) * 60,
        y: prev?.y ?? Math.sin(angle) * 220 + (Math.random() - 0.5) * 60,
      });
    });
    // Edges drive the layout's attraction only (undirected, deduped); the canvas
    // draws links — with direction — from the `links` prop, not from these edges.
    for (const link of links) {
      const { src_node_id: s, dst_node_id: t } = link;
      if (s === t || !g.hasNode(s) || !g.hasNode(t) || g.hasEdge(s, t))
        continue;
      g.addEdge(s, t, { type: link.link_type });
    }
    if (g.order > 1 && g.size > 0) {
      try {
        louvain.assign(g, { nodeCommunityAttribute: "community" });
      } catch {
        // Communities are decorative (far-zoom regions); never block the map.
      }
    }
    return g;
  }, [nodes, links]);

  useEffect(() => {
    if (graph.order === 0) return;

    const cachePositions = () => {
      graph.forEachNode((id, attr) => {
        positions.current.set(id, { x: attr.x as number, y: attr.y as number });
      });
    };

    // Spread settings — strong repulsion + gentle gravity open the map into
    // readable regions rather than a tight clump (the baseline's strong-repulsion
    // feel). Config-driven + B-phase-tunable (K5-D-2 / Group E).
    const settings = {
      ...forceAtlas2.inferSettings(graph),
      barnesHutOptimize: graph.order > BARNES_HUT_THRESHOLD,
      scalingRatio: 140,
      gravity: 0.8,
      adjustSizes: true,
      slowDown: 6,
    };

    // Tiny graphs + reduced-motion settle synchronously — calm, no live worker.
    if (reducedMotion || graph.order < 3) {
      forceAtlas2.assign(graph, { iterations: 420, settings });
      cachePositions();
      return;
    }

    const layout = new FA2LayoutSupervisor(graph, { settings });
    layout.start();
    const settle = setTimeout(() => layout.stop(), SETTLE_MS);

    return () => {
      clearTimeout(settle);
      cachePositions();
      layout.kill();
    };
  }, [graph, reducedMotion]);

  return graph;
}
