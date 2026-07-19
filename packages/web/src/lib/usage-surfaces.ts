/**
 * Spec M3 — all-surface usage grouping.
 *
 * The `GET /v1/me/usage/ledger` endpoint returns one {@link LedgerEntry} per
 * billed event across EVERY surface (chat, authoring, image, voice, agentic
 * runs, task legs, background LLM, sandbox). Each row's `reason` is
 * `"<surface>[:<basis>]"`. This module folds that raw ledger into a stable,
 * ordered per-surface breakdown the settings page renders.
 *
 * The mapping mirrors the backend's OWN conceptual grouping (see the
 * `LedgerEntry` docstring in `responses.py`): voice-autopick, episodic
 * consolidation, and initiative scans are "background LLM", not their own
 * surfaces — so `voice_pick` folds into `background`, not `voice`.
 *
 * Pure + framework-free so it is unit-testable without a render.
 */
import type { components } from "@/lib/api/schema";

export type LedgerEntry = components["schemas"]["LedgerEntry"];

export type SurfaceKey =
  | "chat"
  | "authoring"
  | "image"
  | "voice"
  | "agentic"
  | "task"
  | "background"
  | "sandbox"
  | "other";

/** Fixed display order — the order surfaces appear in the breakdown. */
export const SURFACE_ORDER: readonly SurfaceKey[] = [
  "chat",
  "authoring",
  "image",
  "voice",
  "agentic",
  "task",
  "background",
  "sandbox",
  "other",
] as const;

/**
 * Map a ledger `reason` to its user-facing surface bucket.
 *
 * Matches on the surface segment (the part before the first `:` — the basis is
 * stripped). Order is significant: `voice_pick` is checked before the generic
 * `voice` prefix so voice-autopick lands in `background` (the backend bills it
 * as a background LLM call), not real-time `voice`.
 */
export function surfaceKey(reason: string): SurfaceKey {
  const head = reason.split(":", 1)[0] ?? "";
  if (head.startsWith("chat")) return "chat";
  if (head.includes("authoring")) return "authoring";
  if (head.startsWith("agentic")) return "agentic";
  if (head.startsWith("task")) return "task";
  if (head.startsWith("sandbox")) return "sandbox";
  if (head.startsWith("image") || head.startsWith("avatar")) return "image";
  // voice-autopick is a background LLM charge, not real-time voice.
  if (head.startsWith("voice_pick")) return "background";
  if (head.startsWith("voice")) return "voice";
  if (
    head.startsWith("episodic") ||
    head.startsWith("initiative") ||
    head.startsWith("embeddings") ||
    head.startsWith("connectors") ||
    head.startsWith("background")
  ) {
    return "background";
  }
  return "other";
}

export interface SurfaceGroup {
  readonly key: SurfaceKey;
  /** Number of ledger events folded into this surface. */
  readonly count: number;
  /**
   * Net credits charged: `−delta` summed across the surface's rows. Charges are
   * negative deltas, so this is a positive "spend"; a refund (positive delta)
   * nets it back down.
   */
  readonly creditsCharged: number;
  /** Summed provider cost in cents across rows that carried one. */
  readonly costCents: number;
  /** True if ANY row carried a `cost_cents` (distinguishes a real `0` from unpriced). */
  readonly hasCost: boolean;
  /**
   * The provenance basis: the single `cost_basis` when every row agrees,
   * `"mixed"` when they differ, or `null` when every row is legacy (no basis).
   */
  readonly basis: string | null;
}

export interface LedgerTotals {
  readonly creditsCharged: number;
  readonly costCents: number;
  readonly hasCost: boolean;
  readonly count: number;
}

interface Acc {
  count: number;
  credits: number;
  cost: number;
  hasCost: boolean;
  bases: Set<string>;
}

/**
 * Fold a raw ledger into ordered per-surface groups. Only surfaces that
 * actually have events appear, returned in {@link SURFACE_ORDER}.
 */
export function aggregateBySurface(
  entries: readonly LedgerEntry[],
): SurfaceGroup[] {
  const acc = new Map<SurfaceKey, Acc>();
  for (const entry of entries) {
    const key = surfaceKey(entry.reason);
    const g: Acc = acc.get(key) ?? {
      count: 0,
      credits: 0,
      cost: 0,
      hasCost: false,
      bases: new Set<string>(),
    };
    g.count += 1;
    g.credits += -entry.delta;
    if (entry.cost_cents != null) {
      g.cost += entry.cost_cents;
      g.hasCost = true;
    }
    if (entry.cost_basis) {
      g.bases.add(entry.cost_basis);
    }
    acc.set(key, g);
  }

  const groups: SurfaceGroup[] = [];
  for (const key of SURFACE_ORDER) {
    const g = acc.get(key);
    if (!g) {
      continue;
    }
    const basis =
      g.bases.size === 0
        ? null
        : g.bases.size === 1
          ? [...g.bases][0]
          : "mixed";
    groups.push({
      key,
      count: g.count,
      creditsCharged: g.credits,
      costCents: g.cost,
      hasCost: g.hasCost,
      basis: basis ?? null,
    });
  }
  return groups;
}

/** Sum a set of surface groups into one row of totals. */
export function totalsOf(groups: readonly SurfaceGroup[]): LedgerTotals {
  return groups.reduce<LedgerTotals>(
    (t, g) => ({
      creditsCharged: t.creditsCharged + g.creditsCharged,
      costCents: t.costCents + g.costCents,
      hasCost: t.hasCost || g.hasCost,
      count: t.count + g.count,
    }),
    { creditsCharged: 0, costCents: 0, hasCost: false, count: 0 },
  );
}

/**
 * The relative share (0–1) of a surface's credits against the total — the width
 * driver for the breakdown's proportional spend bar. Guards a zero total.
 */
export function creditShare(group: SurfaceGroup, total: LedgerTotals): number {
  if (total.creditsCharged <= 0) {
    return 0;
  }
  return Math.max(0, group.creditsCharged) / total.creditsCharged;
}
