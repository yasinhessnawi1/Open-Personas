/**
 * Spec K5 — the Memory graph's visual encoding (K5-D-3), as pure data.
 *
 * Node colour by the seven K0 `NodeKind`s and the four `LinkType`s are concrete
 * `oklch()` values here — the single tunable source for the palette (the design
 * gate, Group E, re-tunes in this one file). They are theme-INDEPENDENT by design
 * (the `--store-*` precedent: L≈0.6–0.68 reads on both the light-paper and the
 * dark canvas), which is also why they live here rather than as `--mem-*` CSS
 * custom properties: those are only ever read at runtime (canvas `getComputedStyle`
 * / inline `var()`), so Tailwind v4's static scan can't see them and tree-shakes
 * them out of `:root`. The genuinely theme-aware surface colours (canvas
 * background, label foreground) still come from CSS tokens, resolved in the canvas.
 *
 * Kept framework-free + side-effect-free so the mapping stays unit-testable.
 */

/** The seven node kinds K0 emits (`persona.graph.models.NodeKind`). */
export type NodeKind =
  | "concept"
  | "fact"
  | "preference"
  | "trait"
  | "goal"
  | "circumstance"
  | "entity";

/** The four typed links K0 emits (`persona.graph.models.LinkType`). */
export type LinkType = "semantic" | "entity" | "temporal" | "causal";

/**
 * NodeKind → colour. ENTITY is visually distinct (warm gold — a person/place is a
 * different KIND of node); the other six sit on a restrained, low-chroma editorial
 * ramp so the map reads as a calm asset, not a loud dossier (K5-D-3). `kindColor`
 * falls back to the concept tone for any unknown (future) kind.
 */
const KIND_COLOR: Record<NodeKind, string> = {
  concept: "oklch(0.66 0.13 250)",
  fact: "oklch(0.66 0.13 210)",
  preference: "oklch(0.67 0.14 162)",
  trait: "oklch(0.67 0.15 138)",
  goal: "oklch(0.7 0.15 62)",
  circumstance: "oklch(0.64 0.15 330)",
  entity: "oklch(0.74 0.14 78)",
};

export function kindColor(kind: string): string {
  return KIND_COLOR[kind as NodeKind] ?? KIND_COLOR.concept;
}

/** The K4 sensitive mark — a soft ring of care, never a warning hue (K5-D-10). */
export const CARE_COLOR = "oklch(0.66 0.15 300)";

/**
 * The per-link-type drawing recipe (the baseline's typed-link language):
 * semantic = quiet dotted texture · entity = solid thread · temporal = dashed ·
 * causal = weighted + a direction arrow.
 */
export interface LinkEncoding {
  readonly color: string;
  readonly width: number;
  readonly dash: readonly number[];
  readonly arrow: boolean;
  /** Resting length for the force layout — entity threads pull tighter. */
  readonly restLength: number;
  /** Base opacity when not dimmed. */
  readonly alpha: number;
}

const LINK_ENCODING: Record<LinkType, LinkEncoding> = {
  semantic: {
    color: "oklch(0.7 0.02 250)",
    width: 1,
    dash: [2, 4],
    arrow: false,
    restLength: 150,
    alpha: 0.35,
  },
  entity: {
    color: "oklch(0.74 0.14 78)",
    width: 1.8,
    dash: [],
    arrow: false,
    restLength: 70,
    alpha: 0.9,
  },
  temporal: {
    color: "oklch(0.66 0.16 140)",
    width: 1.6,
    dash: [5, 5],
    arrow: false,
    restLength: 110,
    alpha: 0.8,
  },
  causal: {
    color: "oklch(0.62 0.22 27)",
    width: 2.4,
    dash: [],
    arrow: true,
    restLength: 95,
    alpha: 0.95,
  },
};

const FALLBACK_LINK: LinkEncoding = LINK_ENCODING.semantic;

export function linkEncoding(type: string): LinkEncoding {
  return LINK_ENCODING[type as LinkType] ?? FALLBACK_LINK;
}

/**
 * Node radius by degree — connectedness, lightly (the baseline's `5 + deg·0.95`,
 * clamped 5–16). Size reads as "how connected", never "how important" (K5-D-3).
 */
export function nodeRadius(degree: number): number {
  return Math.max(5, Math.min(19, 5 + Math.max(0, degree) * 1.15));
}

/**
 * Detail-panel link ordering — structure first (causal → entity → temporal →
 * semantic), so the meaningful threads lead and the quiet semantic relations
 * trail (the baseline's sort).
 */
const LINK_SORT_RANK: Record<LinkType, number> = {
  causal: 0,
  entity: 1,
  temporal: 2,
  semantic: 3,
};

export function linkSortRank(type: string): number {
  return LINK_SORT_RANK[type as LinkType] ?? 99;
}

/** i18n key suffix for a link's human relation label (`memory.rel.<key>`). */
export function linkRelationKey(type: string): string {
  return (type as LinkType) in LINK_ENCODING ? type : "semantic";
}
