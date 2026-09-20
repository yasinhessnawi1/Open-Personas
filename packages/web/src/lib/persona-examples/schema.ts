/**
 * Shared schema, palettes, and builders for the starter-persona roster
 * (Spec 36, restructured for the roster v2 redesign).
 *
 * ONE roster, two uses (D-36-roster / D-36-seed-field):
 *   1. PRIMARY - `structure` is a complete, valid v1.0 persona the user picks,
 *      edits in place, and creates DIRECTLY via `POST /v1/personas` (no `/author`
 *      LLM call). This is the flagship, capability-rich starter set.
 *   2. SECONDARY - `seed` is a short description for the drafter's "describe your
 *      own" path; it is DERIVED from / aligned with the same identity (a
 *      coherence test asserts name+role agree), so there is no divergent second
 *      cast of example personas.
 *
 * The craft bar (spec section 3, criterion 8): each starter reads as "oh, *that*
 * persona can do **that**, I want it." Every wired capability in `structure`
 * (`tools` / `skills` / `mcp:*`) is drawn ONLY from the live catalogs (the
 * palettes below); ambition beyond the shipped catalogs (third-party MCP
 * integrations, home automation, workspace connectors) appears ONLY as plain
 * "Roadmap:" prose in `background`, never as functional wiring
 * (D-36-honesty-rule). A dataset-integrity test enforces both, see
 * persona-examples.test.ts.
 *
 * Accent: each category binds to one of the four typed-memory store hues (plus
 * the vermilion core), expressed as OKLCH and consumed via `--accent-*` custom
 * properties (token-resolved; no literal colors in component class names).
 */

import { SAFETY_CONSTRAINT } from "@/lib/persona-safety";

/**
 * The live capability palettes, the ONLY identifiers a starter may wire.
 *
 * These mirror the server catalogs that ship TODAY:
 *   tools  -> packages/core/src/persona/tools/catalog.py
 *   skills -> packages/core/src/persona/skills/catalog.toml
 *   mcp    -> packages/core/src/persona/tools/mcp/catalog.toml
 * The dataset-integrity test asserts every wired id is a member, so a typo or a
 * faked capability fails CI (D-36-honesty-rule). `mcp:fetch` is deliberately
 * ABSENT (SSRF-unpatched, high-risk; use in-tree `web_fetch`); `mcp_search` is
 * absent because it is not runtime-wired yet.
 */
export const TOOL_PALETTE = [
  "web_search",
  "web_fetch",
  "file_read",
  "file_write",
  "code_execution",
  "calculator",
  "datetime",
  "regex_match",
  "text_diff",
  "text_summarize",
  "json_query",
  "currency_convert",
  "generate_image",
  "render_diagram",
] as const;

export const SKILL_PALETTE = [
  "web_research",
  "data_analysis",
  "document_generation",
  "code_review",
] as const;

/** Built-in MCP servers, wired as `mcp:<name>` entries in a persona's `tools`. */
export const MCP_PALETTE = [
  "mcp:time",
  "mcp:calculator",
  "mcp:filesystem",
  "mcp:weather",
  "mcp:github",
] as const;

/** The full set of legal `tools` entries (in-tree tools + `mcp:*` servers). */
export const WIRABLE_TOOLS: readonly string[] = [
  ...TOOL_PALETTE,
  ...MCP_PALETTE,
];

export type EpistemicStatus = "fact" | "belief" | "hypothesis" | "contested";

/** A self-fact line in a starter's typed memory. */
export interface SelfFactSeed {
  fact: string;
  confidence: number;
}

/** A worldview claim line in a starter's typed memory. */
export interface WorldviewSeed {
  claim: string;
  domain: string;
  epistemic: EpistemicStatus;
  confidence: number;
}

/**
 * How a starter shows up: the authored answer its portrait and its voice both
 * read, instead of each guessing separately (R9-155).
 *
 * Mirrors `PersonaPresentation` in
 * `packages/core/src/persona/schema/persona.py`. Two rules govern what goes in
 * here, and both exist because seventy odd judgement calls is a lot of chances
 * to invent a stereotype:
 *
 * 1. `presents` is set ONLY where the starter's own background already says
 *    it, by the pronoun that text uses for the persona. Where the text keeps
 *    the persona ungendered, the honest answer is `unspecified`, and the voice
 *    picker then chooses on character alone exactly as it does today.
 * 2. No `appearance` prose for any starter. Nothing in this roster describes
 *    what a persona looks like, so nothing here should invent it.
 */
export interface PersonaPresentationSeed {
  /**
   * `synthetic` for a persona a human portrait would misrepresent: the three
   * cinema-AI flagships, the roommate its own text calls virtual, and the
   * journaling guide its own text calls "it". They are drawn as their own
   * generated mark instead. Everyone else is `human`, which is exactly the
   * behaviour every starter had before this field existed.
   */
  form: "human" | "synthetic";
  /** The persona's gender presentation, or `unspecified` when its text does not say. */
  presents: "feminine" | "masculine" | "neutral" | "unspecified";
}

/**
 * A complete, valid v1.0 persona document, the editable draft a starter
 * populates. Mirrors `packages/core/src/persona/schema/persona.py`; serialised
 * to YAML (via `docToYaml`) and posted straight to `POST /v1/personas`.
 */
export interface PersonaStructure {
  schema_version: "1.0";
  identity: {
    name: string;
    role: string;
    background: string;
    /** ISO 639-1 code the persona SPEAKS TO ITS USERS. */
    language_default: string;
    /** Hard constraints; index 0 is always the verbatim safety constraint. */
    constraints: string[];
    /** Authored, never guessed: see {@link PersonaPresentationSeed}. */
    presentation: PersonaPresentationSeed;
  };
  self_facts: SelfFactSeed[];
  worldview: WorldviewSeed[];
  /** In-tree tool names + `mcp:<server>` entries, all from WIRABLE_TOOLS. */
  tools: string[];
  /** Skill-pack names, all from SKILL_PALETTE. */
  skills: string[];
  /** Automatic routing defaults on for new personas (set explicitly here). */
  routing: { intelligent: { enabled: true } };
}

/** A single starter persona shown as a card in the gallery. */
export interface PersonaExample {
  /** Stable id (used as React key + selection signal). */
  id: string;
  /** Distinctive persona name (the display headline of the card). */
  name: string;
  /** One-line role/title. */
  role: string;
  /** A short, evocative hook (one sentence, no period needed). */
  hook: string;
  /** The seed description written into the describe textarea on pick (drafter path). */
  seed: string;
  /** The full structured persona for the primary direct-create path. */
  structure: PersonaStructure;
}

/** A named group of starter personas with a brand-store accent. */
export interface PersonaExampleCategory {
  /** Stable id (React key + i18n label lookup). */
  id: string;
  /**
   * Brand-store accent for the category. Maps to a typed-memory store hue:
   *   identity (teal) · self_facts (green) · worldview (indigo) ·
   *   episodic (rose) · core (vermilion).
   * Resolved to OKLCH via `ACCENT_OKLCH` at render; never a literal class.
   */
  accent: "core" | "identity" | "self_facts" | "worldview" | "episodic";
  /**
   * Marks the flagship shelf: rendered first with the hero treatment (larger
   * cards, hook line visible). Exactly one category carries this flag.
   */
  featured?: boolean;
  examples: PersonaExample[];
}

/**
 * OKLCH components per accent, mirroring the brand store-node hues documented
 * in public/brand/README.md and the tier/chart hues in globals.css. Applied as
 * inline `--accent-*` custom properties so cards tint without hard-coded color
 * utilities (keeps the no-literals gate green).
 */
export const ACCENT_OKLCH: Record<
  PersonaExampleCategory["accent"],
  { h: number; c: number; l: number }
> = {
  // Vermilion brand core (== --primary / --tier-frontier).
  core: { h: 30, c: 0.196, l: 0.585 },
  // identity · teal
  identity: { h: 185, c: 0.09, l: 0.6 },
  // self_facts · green (== --chart-4 family)
  self_facts: { h: 145, c: 0.09, l: 0.55 },
  // worldview · indigo (== --tier-small slate-indigo family)
  worldview: { h: 264, c: 0.1, l: 0.6 },
  // episodic · rose (== --chart-5 family)
  episodic: { h: 350, c: 0.11, l: 0.6 },
};

/** Routing block shared by every starter (automatic routing on, D-36-routing-explicit). */
const ROUTING_ON = { intelligent: { enabled: true } } as const;

/**
 * Build a starter `structure`, pinning `schema_version` and prepending the
 * verbatim safety constraint so it is always the first constraint (the dataset
 * mirror of the create-boundary guard; a test asserts it on every starter).
 *
 * `presentation` is REQUIRED rather than defaulted on purpose: a new starter
 * cannot be added without someone deciding whether it is a person, which is a
 * compile-time version of the guarantee the dataset test checks at runtime. A
 * default would silently make the next JARVIS a human face again.
 */
export function structure(s: {
  name: string;
  role: string;
  background: string;
  language_default?: string;
  presentation: PersonaPresentationSeed;
  constraints: string[];
  self_facts: SelfFactSeed[];
  worldview: WorldviewSeed[];
  tools: string[];
  skills: string[];
}): PersonaStructure {
  return {
    schema_version: "1.0",
    identity: {
      name: s.name,
      role: s.role,
      background: s.background,
      language_default: s.language_default ?? "en",
      constraints: [SAFETY_CONSTRAINT, ...s.constraints],
      presentation: s.presentation,
    },
    self_facts: s.self_facts,
    worldview: s.worldview,
    tools: s.tools,
    skills: s.skills,
    routing: ROUTING_ON,
  };
}
