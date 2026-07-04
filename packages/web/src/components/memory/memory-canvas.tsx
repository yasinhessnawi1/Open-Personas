"use client";

import { Maximize, Minus, Plus } from "lucide-react";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useRef } from "react";
import type { MemoryLinkEdge, MemoryNodeSummary } from "@/lib/api";
import {
  CARE_COLOR,
  kindColor,
  linkEncoding,
  nodeRadius,
} from "@/lib/memory/encoding";
import { useForceLayout } from "@/lib/memory/use-force-layout";

interface Camera {
  x: number;
  y: number;
  scale: number;
}

const FAR_ZOOM = 0.45;
const MIN_SCALE = 0.25;
const MAX_SCALE = 3.2;
// How long after a (re)load the camera tracks the worker's spreading layout.
const SETTLE_FIT_MS = 2800;

/**
 * Spec K5 — the 2-D-canvas force-graph renderer (K5-D-1), a live-data port of
 * `Memory.html`. Positions come from the worker-driven layout (`useForceLayout`);
 * this component owns the camera, the per-frame paint (typed-link encoding,
 * degree-sized nodes, the K4 ring-of-care, far-zoom Louvain region labels), and
 * pointer interaction (pan / zoom / hover / select / drag). Redraws every frame
 * while the layout settles, then on interaction (the loop is cheap at window
 * scale; the heavy force work is off-thread).
 */
export function MemoryCanvas({
  nodes,
  links,
  selectedId,
  onSelectNode,
  reducedMotion,
}: {
  nodes: readonly MemoryNodeSummary[];
  links: readonly MemoryLinkEdge[];
  selectedId: string | null;
  onSelectNode: (id: string | null) => void;
  reducedMotion: boolean;
}) {
  const t = useTranslations("memory");
  const graph = useForceLayout(nodes, links, reducedMotion);

  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const cam = useRef<Camera>({ x: 0, y: 0, scale: 1 });
  const follow = useRef<{ id: string; scale: number } | null>(null);
  const size = useRef<{ w: number; h: number }>({ w: 0, h: 0 });
  const hoverId = useRef<string | null>(null);
  const selectedRef = useRef<string | null>(selectedId);

  // Latest props for the imperative render loop (which closes over refs, not state).
  const dataRef = useRef({ nodes, links });
  dataRef.current = { nodes, links };
  selectedRef.current = selectedId;

  const cssCache = useRef<Map<string, string>>(new Map());
  const css = useCallback((name: string): string => {
    const cached = cssCache.current.get(name);
    if (cached !== undefined) return cached;
    const value = getComputedStyle(document.documentElement)
      .getPropertyValue(name)
      .trim();
    cssCache.current.set(name, value);
    return value;
  }, []);

  const toScreen = useCallback((wx: number, wy: number) => {
    const c = cam.current;
    const { w, h } = size.current;
    return { x: (wx - c.x) * c.scale + w / 2, y: (wy - c.y) * c.scale + h / 2 };
  }, []);
  const toWorld = useCallback((sx: number, sy: number) => {
    const c = cam.current;
    const { w, h } = size.current;
    return { x: (sx - w / 2) / c.scale + c.x, y: (sy - h / 2) / c.scale + c.y };
  }, []);

  const pos = useCallback(
    (id: string): { x: number; y: number } | null => {
      if (!graph.hasNode(id)) return null;
      return {
        x: graph.getNodeAttribute(id, "x") as number,
        y: graph.getNodeAttribute(id, "y") as number,
      };
    },
    [graph],
  );

  const neighbourSet = useCallback((id: string): Set<string> => {
    const set = new Set<string>([id]);
    for (const link of dataRef.current.links) {
      if (link.src_node_id === id) set.add(link.dst_node_id);
      if (link.dst_node_id === id) set.add(link.src_node_id);
    }
    return set;
  }, []);

  const fit = useCallback(() => {
    const live = dataRef.current.nodes;
    if (live.length === 0) return;
    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;
    for (const n of live) {
      const p = pos(n.id);
      if (!p) continue;
      minX = Math.min(minX, p.x);
      minY = Math.min(minY, p.y);
      maxX = Math.max(maxX, p.x);
      maxY = Math.max(maxY, p.y);
    }
    if (!Number.isFinite(minX)) return;
    const { w, h } = size.current;
    // Fit the bounding box to ~76% of the viewport (a 12% margin each side) so the
    // constellation fills the canvas instead of sitting as a small clump. Clamped
    // so a tiny graph doesn't zoom absurdly and a huge one still frames.
    const margin = 0.12;
    const spanX = maxX - minX || 1;
    const spanY = maxY - minY || 1;
    const sc = Math.min(
      (w * (1 - 2 * margin)) / spanX,
      (h * (1 - 2 * margin)) / spanY,
    );
    follow.current = null;
    cam.current = {
      x: (minX + maxX) / 2,
      y: (minY + maxY) / 2,
      scale: Math.max(0.2, Math.min(sc || 1, 2.6)),
    };
  }, [pos]);

  // --- the render loop -------------------------------------------------------
  // Re-runs whenever the layout `graph` changes (a new/expanded window rebuilds
  // it); `startT` (below) restarts the settle-fit window for the fresh node set.
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);

    const resize = () => {
      const rect = canvas.getBoundingClientRect();
      size.current = { w: rect.width, h: rect.height };
      canvas.width = rect.width * dpr;
      canvas.height = rect.height * dpr;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };
    resize();
    const ro = new ResizeObserver(resize);
    if (canvas.parentElement) ro.observe(canvas.parentElement);

    let raf = 0;
    const draw = () => {
      cssCache.current.clear(); // re-resolve tokens each frame (cheap; theme-safe)
      const { w, h } = size.current;
      const { nodes: ns, links: ls } = dataRef.current;
      const c = cam.current;

      // camera easing toward the followed (selected/searched) node
      if (follow.current) {
        const p = pos(follow.current.id);
        if (p) {
          const e = reducedMotion ? 1 : 0.14;
          c.x += (p.x - c.x) * e;
          c.y += (p.y - c.y) * e;
          c.scale += (follow.current.scale - c.scale) * e;
        }
      }

      ctx.clearRect(0, 0, w, h);
      ctx.fillStyle = css("--background");
      ctx.fillRect(0, 0, w, h);

      const focusId = hoverId.current ?? selectedRef.current;
      const nb = focusId ? neighbourSet(focusId) : null;
      const farZoom = c.scale < FAR_ZOOM;

      // far-zoom region labels (K5-R-2): one label per Louvain community centroid
      if (farZoom) {
        const agg = new Map<number, { x: number; y: number; n: number }>();
        for (const n of ns) {
          if (!graph.hasNode(n.id)) continue;
          const community = graph.getNodeAttribute(n.id, "community") as
            | number
            | undefined;
          if (community === undefined) continue;
          const p = pos(n.id);
          if (!p) continue;
          const a = agg.get(community) ?? { x: 0, y: 0, n: 0 };
          a.x += p.x;
          a.y += p.y;
          a.n += 1;
          agg.set(community, a);
        }
        const tFade = Math.max(0, Math.min(1, (FAR_ZOOM - c.scale) / 0.25));
        ctx.fillStyle = css("--muted-foreground");
        ctx.font = "600 13px ui-sans-serif, system-ui, sans-serif";
        ctx.textAlign = "center";
        for (const a of agg.values()) {
          if (a.n < 2) continue;
          const sp = toScreen(a.x / a.n, a.y / a.n);
          ctx.globalAlpha = tFade * 0.9;
          ctx.fillText(t("region", { count: a.n }), sp.x, sp.y);
        }
        ctx.globalAlpha = 1;
      }

      // links
      for (const link of ls) {
        const sp = pos(link.src_node_id);
        const tp = pos(link.dst_node_id);
        if (!sp || !tp) continue;
        const a = toScreen(sp.x, sp.y);
        const b = toScreen(tp.x, tp.y);
        const enc = linkEncoding(link.link_type);
        const connected =
          nb && (link.src_node_id === focusId || link.dst_node_id === focusId);
        const dim = Boolean(focusId) && !connected;
        ctx.strokeStyle = enc.color;
        ctx.lineWidth = enc.width;
        ctx.globalAlpha = dim ? enc.alpha * 0.18 : enc.alpha;
        ctx.setLineDash(enc.dash as number[]);
        ctx.beginPath();
        ctx.moveTo(a.x, a.y);
        ctx.lineTo(b.x, b.y);
        ctx.stroke();
        if (enc.arrow && !dim && c.scale > 0.5) {
          const ang = Math.atan2(b.y - a.y, b.x - a.x);
          // place the arrowhead ~62% toward the target so direction reads clearly
          const mx = a.x + (b.x - a.x) * 0.62;
          const my = a.y + (b.y - a.y) * 0.62;
          const s = 8;
          ctx.lineWidth = enc.width;
          ctx.setLineDash([]);
          ctx.beginPath();
          ctx.moveTo(mx, my);
          ctx.lineTo(
            mx - s * Math.cos(ang - 0.5),
            my - s * Math.sin(ang - 0.5),
          );
          ctx.moveTo(mx, my);
          ctx.lineTo(
            mx - s * Math.cos(ang + 0.5),
            my - s * Math.sin(ang + 0.5),
          );
          ctx.stroke();
        }
      }
      ctx.setLineDash([]);
      ctx.globalAlpha = 1;

      // nodes
      const zoomR = Math.max(0.7, Math.min(1.4, c.scale * 0.9 + 0.3));
      for (const n of ns) {
        const p = pos(n.id);
        if (!p) continue;
        const sp = toScreen(p.x, p.y);
        const r = nodeRadius(n.degree) * zoomR;
        const isFocus = focusId === n.id;
        const inNb = nb?.has(n.id) ?? false;
        const dim = Boolean(focusId) && !inNb;
        const colour = kindColor(n.kind);
        const baseAlpha = dim ? 0.22 : 1;

        if (isFocus || selectedRef.current === n.id) {
          ctx.beginPath();
          ctx.arc(sp.x, sp.y, r + 7, 0, Math.PI * 2);
          ctx.fillStyle = colour;
          ctx.globalAlpha = baseAlpha * 0.18;
          ctx.fill();
        }
        ctx.globalAlpha = baseAlpha;
        ctx.beginPath();
        ctx.arc(sp.x, sp.y, r, 0, Math.PI * 2);
        ctx.fillStyle = colour;
        ctx.fill();
        ctx.lineWidth = 1.5;
        ctx.strokeStyle = css("--background");
        ctx.stroke();

        // the K4 sensitive mark — a soft ring of care, never a warning badge
        if (n.wellbeing_category) {
          ctx.beginPath();
          ctx.arc(sp.x, sp.y, r + 3.5, -0.6, 2.4);
          ctx.strokeStyle = CARE_COLOR;
          ctx.lineWidth = 1.6;
          ctx.globalAlpha = baseAlpha * 0.9;
          ctx.stroke();
        }

        ctx.globalAlpha = 1;
      }

      // labels — degree-priority with collision avoidance (K5-D-3 legibility):
      // the focus + its neighbourhood label first and always; the rest draw by
      // degree (hubs win) and a low-degree label yields when it would overlap one
      // already placed. As zoom spreads the nodes, fewer collide → more reveal.
      if (!farZoom) {
        ctx.textAlign = "center";
        ctx.lineJoin = "round";
        const placed: { x: number; y: number; w: number; h: number }[] = [];
        const candidates = ns
          .filter((node) => !(focusId && !(nb?.has(node.id) ?? false)))
          .map((node) => ({
            node,
            priority:
              focusId === node.id ? 3 : (nb?.has(node.id) ?? false) ? 2 : 0,
          }))
          .sort(
            (a, b) => b.priority - a.priority || b.node.degree - a.node.degree,
          );
        for (const { node, priority } of candidates) {
          const p = pos(node.id);
          if (!p) continue;
          const sp = toScreen(p.x, p.y);
          const isFocus = focusId === node.id;
          const r = nodeRadius(node.degree) * zoomR;
          ctx.font = `${isFocus ? "600" : "500"} 12px ui-sans-serif, system-ui, sans-serif`;
          const tw = ctx.measureText(node.label).width;
          const ly = sp.y + r + 13;
          const rect = { x: sp.x - tw / 2 - 2, y: ly - 11, w: tw + 4, h: 15 };
          const collides = placed.some(
            (d) =>
              rect.x < d.x + d.w &&
              rect.x + rect.w > d.x &&
              rect.y < d.y + d.h &&
              rect.y + rect.h > d.y,
          );
          if (collides && priority < 3) continue; // focus always labels
          placed.push(rect);
          ctx.lineWidth = 3;
          ctx.strokeStyle = css("--background");
          ctx.strokeText(node.label, sp.x, ly);
          ctx.fillStyle = css("--foreground");
          ctx.fillText(node.label, sp.x, ly);
        }
      }

      // Track the spreading layout: re-fit every frame during the settle window
      // (the worker moves nodes off their seed clump over ~2.6s), then hand the
      // camera to the user. A manual pan/zoom/select cancels follow but not this
      // fit; the window is short and only runs right after a (re)load.
      if (ns.length > 0 && performance.now() - startT < SETTLE_FIT_MS) fit();

      raf = requestAnimationFrame(draw);
    };
    const startT = performance.now();
    raf = requestAnimationFrame(draw);

    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
    };
  }, [graph, pos, css, toScreen, neighbourSet, fit, reducedMotion, t]);

  // fly to the selected node whenever it changes (click or search)
  useEffect(() => {
    if (selectedId && graph.hasNode(selectedId)) {
      follow.current = {
        id: selectedId,
        scale: Math.max(1.15, cam.current.scale),
      };
    }
  }, [selectedId, graph]);

  // --- pointer interaction ---------------------------------------------------
  const drag = useRef<{
    id: string | null;
    panning: boolean;
    moved: boolean;
    last: { x: number; y: number };
  }>({
    id: null,
    panning: false,
    moved: false,
    last: { x: 0, y: 0 },
  });

  const pick = useCallback(
    (sx: number, sy: number): string | null => {
      let best: string | null = null;
      let bestD = 18;
      for (const n of dataRef.current.nodes) {
        const p = pos(n.id);
        if (!p) continue;
        const sp = toScreen(p.x, p.y);
        const d = Math.hypot(sp.x - sx, sp.y - sy);
        const r = nodeRadius(n.degree) + 6;
        if (d < r && d < bestD) {
          bestD = d;
          best = n.id;
        }
      }
      return best;
    },
    [pos, toScreen],
  );

  const onPointerDown = (e: React.PointerEvent<HTMLCanvasElement>) => {
    e.currentTarget.setPointerCapture(e.pointerId);
    const id = pick(e.nativeEvent.offsetX, e.nativeEvent.offsetY);
    drag.current = {
      id,
      panning: id === null,
      moved: false,
      last: { x: e.nativeEvent.offsetX, y: e.nativeEvent.offsetY },
    };
    if (id) follow.current = null;
  };
  const onPointerMove = (e: React.PointerEvent<HTMLCanvasElement>) => {
    const d = drag.current;
    const ox = e.nativeEvent.offsetX;
    const oy = e.nativeEvent.offsetY;
    if (d.id) {
      const w = toWorld(ox, oy);
      if (graph.hasNode(d.id)) {
        graph.setNodeAttribute(d.id, "x", w.x);
        graph.setNodeAttribute(d.id, "y", w.y);
      }
      d.moved = true;
    } else if (d.panning) {
      const c = cam.current;
      c.x -= (ox - d.last.x) / c.scale;
      c.y -= (oy - d.last.y) / c.scale;
      d.last = { x: ox, y: oy };
      d.moved = true;
      follow.current = null;
    } else {
      hoverId.current = pick(ox, oy);
    }
  };
  const onPointerUp = () => {
    const d = drag.current;
    if (d.id && !d.moved) onSelectNode(d.id);
    else if (d.panning && !d.moved) onSelectNode(null);
    drag.current = {
      id: null,
      panning: false,
      moved: false,
      last: { x: 0, y: 0 },
    };
  };
  const onWheel = (e: React.WheelEvent<HTMLCanvasElement>) => {
    follow.current = null;
    const c = cam.current;
    const before = toWorld(e.nativeEvent.offsetX, e.nativeEvent.offsetY);
    c.scale = Math.max(
      MIN_SCALE,
      Math.min(MAX_SCALE, c.scale * 1.0015 ** -e.deltaY),
    );
    const after = toWorld(e.nativeEvent.offsetX, e.nativeEvent.offsetY);
    c.x += before.x - after.x;
    c.y += before.y - after.y;
  };

  const zoomBy = (factor: number) => {
    follow.current = null;
    cam.current.scale = Math.max(
      MIN_SCALE,
      Math.min(MAX_SCALE, cam.current.scale * factor),
    );
  };

  return (
    <div className="relative size-full">
      <canvas
        ref={canvasRef}
        className="size-full touch-none"
        style={{ cursor: "grab" }}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onWheel={onWheel}
      />
      <div className="absolute left-4 top-4 flex flex-col overflow-hidden rounded-md shadow-[var(--elevation-1)]">
        <button
          type="button"
          aria-label={t("zoomIn")}
          onClick={() => zoomBy(1.3)}
          className="grid size-9 place-items-center border-b bg-card text-foreground hover:bg-muted"
        >
          <Plus className="size-4" />
        </button>
        <button
          type="button"
          aria-label={t("zoomOut")}
          onClick={() => zoomBy(1 / 1.3)}
          className="grid size-9 place-items-center border-b bg-card text-foreground hover:bg-muted"
        >
          <Minus className="size-4" />
        </button>
        <button
          type="button"
          aria-label={t("fit")}
          onClick={fit}
          className="grid size-9 place-items-center bg-card text-foreground hover:bg-muted"
        >
          <Maximize className="size-4" />
        </button>
      </div>
    </div>
  );
}
