"use client";

import { Waypoints } from "lucide-react";
import dynamic from "next/dynamic";
import { useTranslations } from "next-intl";
import {
  type ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useState,
} from "react";
import { EmptyState } from "@/components/patterns/empty-state";
import type { AvatarPersona } from "@/components/persona/persona-avatar";
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
import { cn } from "@/lib/utils";
import { EpisodicGraph } from "./episodic-graph";
import { MemoryDetailPanel } from "./memory-detail-panel";
import { MemoryLegend } from "./memory-legend";
import { MemorySearch } from "./memory-search";

type ViewMode = "concept" | "episodic";
const VIEW_MODES: readonly ViewMode[] = ["concept", "episodic"];

/**
 * Spec K11 (T4, D-K11-5) — the `[ Concept | Episodic ]` toggle. A small
 * segmented control (matches the R11-B6 filter-toolbar pattern) — not a route
 * change, so switching stays instant and doesn't lose the concept window's
 * scroll/zoom state when the user comes back to it.
 */
function MemoryModeToggle({
  mode,
  onChange,
}: {
  mode: ViewMode;
  onChange: (mode: ViewMode) => void;
}) {
  const t = useTranslations("memory");
  return (
    <fieldset
      aria-label={t("viewModeLabel")}
      className="inline-flex rounded-lg border border-border bg-muted/40 p-0.5"
    >
      {VIEW_MODES.map((m) => (
        <button
          key={m}
          type="button"
          aria-pressed={mode === m}
          onClick={() => onChange(m)}
          className={cn(
            "rounded-md px-3 py-1.5 text-[13px] font-medium transition-colors",
            mode === m
              ? "bg-background text-foreground shadow-sm"
              : "text-muted-foreground hover:text-foreground",
          )}
        >
          {t(`viewMode.${m}`)}
        </button>
      ))}
    </fieldset>
  );
}

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
  personas = [],
}: {
  memoryWindow: MemoryWindowResponse;
  /**
   * Dev/demo seam (`scratch/memory`): pre-open a node with pre-seeded detail so
   * the editorial panel renders without a live fetch. Omitted in production.
   */
  demoDetail?: MemoryNodeDetail;
  /** The owner's personas — the Episodic tab's per-persona picker (D-K11-5). */
  personas?: readonly AvatarPersona[];
}) {
  const t = useTranslations("memory");
  const api = useApi();

  const [mode, setMode] = useState<ViewMode>("concept");
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

  // The K5 concept view, UNCHANGED (D-K11-5: the toggle only wraps it — an
  // unavailable/empty graph reads as its own honest placeholder, exactly as the
  // /memory page rendered it before the Episodic tab existed).
  let conceptBody: ReactNode;
  if (!memoryWindow.available) {
    conceptBody = (
      <EmptyState
        icon={<Waypoints className="size-8" aria-hidden="true" />}
        title={t("unavailable")}
        description={t("unavailableHint")}
      />
    );
  } else if (nodes.length === 0) {
    conceptBody = (
      <EmptyState
        icon={<Waypoints className="size-8" aria-hidden="true" />}
        title={t("empty")}
        description={t("emptyHint")}
      />
    );
  } else {
    conceptBody = (
      <>
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
      </>
    );
  }

  return (
    <div className="flex flex-1 flex-col">
      <div className="flex items-center gap-2 px-1 pb-3">
        <MemoryModeToggle mode={mode} onChange={setMode} />
      </div>
      {mode === "concept" ? conceptBody : <EpisodicGraph personas={personas} />}
    </div>
  );
}
