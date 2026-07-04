"use client";

import dynamic from "next/dynamic";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useMemo, useState } from "react";
import type { MemoryNodeDetail, MemoryWindowResponse } from "@/lib/api";
import { unwrap } from "@/lib/api";
import { useApi } from "@/lib/api/use-api";
import {
  capWindow,
  type GraphWindow,
  mergeWindow,
  protectedIds,
  removeFromWindow,
} from "@/lib/memory/window-ops";
import { MemoryDetailPanel } from "./memory-detail-panel";
import { MemoryLegend } from "./memory-legend";
import { MemorySearch } from "./memory-search";

// The live drawn set is bounded (K5-D-2): sustained focus-expansion evicts the
// oldest non-focus nodes at this cap so the working set stays in the renderer's
// measured comfort range (~1–1.5k, B1).
const WINDOW_CAP = 1200;

// The canvas owns a Web Worker (FA2 layout) + a <canvas> — client-only, never SSR'd.
const MemoryCanvas = dynamic(
  () => import("./memory-canvas").then((m) => m.MemoryCanvas),
  { ssr: false },
);

/**
 * Spec K5 — the Memory area shell (K5-R-3): the live-data port of `Memory.html`.
 * Composes search-to-fly, the force-graph canvas, the editorial detail panel, and
 * the legend over a single windowed working set (K5-D-2). Selecting/traversing a
 * node that isn't loaded pulls its focus neighbourhood (`GET /graph?focus=`) and
 * merges it in; deletion removes the node + its incident links from the window.
 */
export function MemoryView({
  memoryWindow,
  demoDetail,
}: {
  memoryWindow: MemoryWindowResponse;
  /**
   * Dev/demo seam (`scratch/memory`): pre-open a node with pre-seeded detail so
   * the editorial panel renders without a live fetch. Omitted in production.
   */
  demoDetail?: MemoryNodeDetail;
}) {
  const t = useTranslations("memory");
  const api = useApi();

  const [win, setWin] = useState<GraphWindow>({
    nodes: memoryWindow.nodes,
    links: memoryWindow.links,
  });
  const [total, setTotal] = useState(memoryWindow.total_nodes);
  const [selectedId, setSelectedId] = useState<string | null>(
    demoDetail?.id ?? null,
  );
  const [reducedMotion, setReducedMotion] = useState(false);
  const nodes = win.nodes;
  const links = win.links;

  useEffect(() => {
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    setReducedMotion(mq.matches);
    const onChange = () => setReducedMotion(mq.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  const nodeIds = useMemo(() => new Set(nodes.map((n) => n.id)), [nodes]);

  // Ensure a node is loaded into the window (focus-expansion, K5-D-2), then select
  // it. New neighbours merge in; the working set is then capped (evicting the
  // oldest non-focus nodes) so sustained traversal stays bounded.
  const selectNode = useCallback(
    async (id: string) => {
      if (nodeIds.has(id)) {
        setSelectedId(id);
        return;
      }
      try {
        const incoming = await unwrap(
          await api.GET("/v1/memory/graph", {
            params: { query: { focus: id } },
          }),
        );
        setWin((prev) => {
          const merged = mergeWindow(prev, incoming);
          return capWindow(merged, protectedIds(merged.links, id), WINDOW_CAP);
        });
      } catch {
        // Best-effort expansion; selecting still opens detail for the node.
      }
      setSelectedId(id);
    },
    [api, nodeIds],
  );

  const handleDeleted = useCallback((id: string) => {
    setWin((prev) => removeFromWindow(prev, id));
    setTotal((prev) => Math.max(0, prev - 1));
    setSelectedId(null);
  }, []);

  return (
    <div className="flex flex-1 flex-col">
      <div className="flex items-center gap-4 px-1 pb-3">
        <MemorySearch onSelect={selectNode} />
        <span className="ml-auto inline-flex items-center gap-1.5 rounded-full border bg-card px-3 py-1.5 type-caption normal-case tracking-normal text-muted-foreground tabular-nums">
          <b className="font-medium text-foreground">{total}</b>{" "}
          {t("nodesWord")}
          <span aria-hidden="true">·</span>
          <b className="font-medium text-foreground">{links.length}</b>{" "}
          {t("linksWord")}
        </span>
      </div>

      <div className="relative min-h-[32rem] flex-1 overflow-hidden rounded-xl border bg-background">
        <MemoryCanvas
          nodes={nodes}
          links={links}
          selectedId={selectedId}
          onSelectNode={(id) => (id ? selectNode(id) : setSelectedId(null))}
          reducedMotion={reducedMotion}
        />
        <MemoryLegend />
        <MemoryDetailPanel
          nodeId={selectedId}
          onClose={() => setSelectedId(null)}
          onTraverse={selectNode}
          onDeleted={handleDeleted}
          initialDetail={demoDetail}
        />
      </div>
    </div>
  );
}
