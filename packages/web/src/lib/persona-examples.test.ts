/**
 * Integrity of the curated starter-persona dataset.
 *
 * The gallery seeds the existing author flow from this data, so the shape and
 * uniqueness guarantees matter: ids must be unique (React keys + selection
 * signal), every example must carry a usable seed, and the flat lookup must
 * cover every example.
 */
import { describe, expect, it } from "vitest";
import {
  ACCENT_OKLCH,
  type EpistemicStatus,
  PERSONA_EXAMPLE_CATEGORIES,
  PERSONA_EXAMPLES_BY_ID,
  SKILL_PALETTE,
  WIRABLE_TOOLS,
} from "./persona-examples";
import { SAFETY_CONSTRAINT } from "./persona-safety";

const ALL_EXAMPLES = PERSONA_EXAMPLE_CATEGORIES.flatMap((c) => c.examples);
const EPISTEMIC: ReadonlySet<EpistemicStatus> = new Set([
  "fact",
  "belief",
  "hypothesis",
  "contested",
]);

describe("persona-examples dataset", () => {
  it("has ten categories, featured first, each substantial (~70 total)", () => {
    expect(PERSONA_EXAMPLE_CATEGORIES).toHaveLength(10);
    for (const category of PERSONA_EXAMPLE_CATEGORIES) {
      expect(category.examples.length).toBeGreaterThanOrEqual(
        category.featured ? 3 : 4,
      );
    }
    expect(ALL_EXAMPLES.length).toBeGreaterThanOrEqual(70);
  });

  it("leads with exactly one featured category (the flagship shelf)", () => {
    const featured = PERSONA_EXAMPLE_CATEGORIES.filter((c) => c.featured);
    expect(featured).toHaveLength(1);
    expect(PERSONA_EXAMPLE_CATEGORIES[0]?.featured).toBe(true);
    expect(PERSONA_EXAMPLE_CATEGORIES[0]?.id).toBe("featured");
  });

  it("uses unique ids across all examples and categories", () => {
    const exampleIds = ALL_EXAMPLES.map((e) => e.id);
    expect(new Set(exampleIds).size).toBe(exampleIds.length);

    const categoryIds = PERSONA_EXAMPLE_CATEGORIES.map((c) => c.id);
    expect(new Set(categoryIds).size).toBe(categoryIds.length);
  });

  it("gives every example a name, role, hook, and a substantial seed", () => {
    for (const example of ALL_EXAMPLES) {
      expect(example.name.trim().length).toBeGreaterThan(0);
      expect(example.role.trim().length).toBeGreaterThan(0);
      expect(example.hook.trim().length).toBeGreaterThan(0);
      // Seeds prime the drafter; a one-word seed would be useless.
      expect(example.seed.trim().length).toBeGreaterThan(40);
    }
  });

  it("binds each category to a known accent hue", () => {
    for (const category of PERSONA_EXAMPLE_CATEGORIES) {
      expect(ACCENT_OKLCH[category.accent]).toBeDefined();
    }
  });

  it("exposes a flat lookup covering every example", () => {
    expect(Object.keys(PERSONA_EXAMPLES_BY_ID)).toHaveLength(
      ALL_EXAMPLES.length,
    );
    for (const example of ALL_EXAMPLES) {
      expect(PERSONA_EXAMPLES_BY_ID[example.id]).toEqual(example);
    }
  });

  it("contains no em-dash or en-dash in any visible string", () => {
    // House design rule: hyphen only, never — or –. Covers card chrome AND the
    // structured prose that ships into the persona (background, constraints,
    // self_facts, worldview) — all of it is user-visible.
    for (const example of ALL_EXAMPLES) {
      const s = example.structure;
      const visible = [
        example.name,
        example.role,
        example.hook,
        example.seed,
        s.identity.background,
        ...s.identity.constraints,
        ...s.self_facts.map((f) => f.fact),
        ...s.worldview.map((w) => w.claim),
      ].join(" ");
      expect(visible, example.id).not.toMatch(/[—–]/);
    }
  });

  it("only references MCP servers that exist in the core catalog", () => {
    // Seeds may name an MCP server inline as `mcp:<name>`; every such name must
    // map to a real server in packages/core's BUILTIN_MCP_CATALOG. Keeps the
    // showcase honest — no invented capabilities. (catalog.toml, Spec 27.)
    const KNOWN_MCP_SERVERS = new Set([
      "time",
      "calculator",
      "filesystem",
      "weather",
      "fetch",
      "github",
    ]);
    for (const example of ALL_EXAMPLES) {
      for (const match of example.seed.matchAll(/\bmcp:([a-z_]+)\b/g)) {
        expect(KNOWN_MCP_SERVERS).toContain(match[1]);
      }
    }
  });

  it("collectively exercises a broad spread of real platform capabilities", () => {
    // The starter set should showcase what the platform can actually do, not
    // read as generic chatbots. Each phrase below maps to a shipped capability
    // (tool / skill / MCP / voice / typed memory) verified against the core
    // catalogs; assert the corpus touches every capability family at least once.
    const corpus = ALL_EXAMPLES.map((e) => e.seed.toLowerCase()).join("\n");
    const capabilityFamilies: Record<string, RegExp> = {
      webResearch: /\bresearch|searches?\b|\bweb\b/,
      codeExecution: /code sandbox|run(s)? (in a code|it in)|\bcode\b/,
      codeReview: /review(s|ed)?/,
      documentGeneration: /download(able)?|workbook|document|planner|\bbrief\b/,
      dataAnalysis: /analys(e|es)|\bchart\b|upload(s|ed)?/,
      diagram: /\bdiagram\b/,
      imageGeneration: /\bimage\b|moodboard|concept art/,
      currency: /currenc(y|ies)/,
      voice: /\bvoice\b|out loud/,
      mcp: /\bmcp:/,
      typedMemory: /remember(s|ed)?/,
    };
    for (const [family, pattern] of Object.entries(capabilityFamilies)) {
      expect(corpus, `no example exercises capability: ${family}`).toMatch(
        pattern,
      );
    }
  });
});

describe("prebuilt structured starters (direct-create roster)", () => {
  it("gives every starter a complete, well-formed structure", () => {
    for (const { id, structure: s } of ALL_EXAMPLES) {
      expect(s.schema_version, id).toBe("1.0");
      expect(s.identity.name.trim().length, id).toBeGreaterThan(0);
      expect(s.identity.role.trim().length, id).toBeGreaterThan(0);
      // background is REQUIRED + non-empty in the v1 schema, and is the flagship
      // prose — assert it is substantial, not a one-liner.
      expect(s.identity.background.trim().length, id).toBeGreaterThan(120);
      expect(s.identity.language_default.trim().length, id).toBeGreaterThan(0);
      expect(s.routing.intelligent.enabled, id).toBe(true);
    }
  });

  it("leads every starter's constraints with the verbatim safety constraint", () => {
    // D-36-safety: the dataset mirror of the create-boundary guard.
    for (const { id, structure: s } of ALL_EXAMPLES) {
      expect(s.identity.constraints[0], id).toBe(SAFETY_CONSTRAINT);
      // and never duplicated.
      expect(
        s.identity.constraints.filter((c) => c === SAFETY_CONSTRAINT).length,
        id,
      ).toBe(1);
    }
  });

  it("wires ONLY real, shipped capabilities (the honesty rule)", () => {
    // Every tool/mcp + skill must be a live-catalog member; a faked phase-3
    // capability or a typo fails CI (D-36-honesty-rule).
    for (const { id, structure: s } of ALL_EXAMPLES) {
      for (const tool of s.tools) {
        expect(WIRABLE_TOOLS, `${id} wires unknown tool ${tool}`).toContain(
          tool,
        );
      }
      for (const skill of s.skills) {
        expect(SKILL_PALETTE, `${id} wires unknown skill ${skill}`).toContain(
          skill,
        );
      }
    }
  });

  it("never wires the SSRF-risk mcp:fetch server", () => {
    for (const { id, structure: s } of ALL_EXAMPLES) {
      expect(s.tools, id).not.toContain("mcp:fetch");
    }
  });

  it("validates self_facts and worldview field bounds", () => {
    for (const { id, structure: s } of ALL_EXAMPLES) {
      for (const f of s.self_facts) {
        expect(f.fact.trim().length, id).toBeGreaterThan(0);
        expect(f.confidence, id).toBeGreaterThanOrEqual(0);
        expect(f.confidence, id).toBeLessThanOrEqual(1);
      }
      for (const w of s.worldview) {
        expect(w.claim.trim().length, id).toBeGreaterThan(0);
        expect(EPISTEMIC.has(w.epistemic), `${id} ${w.epistemic}`).toBe(true);
        expect(w.confidence, id).toBeGreaterThanOrEqual(0);
        expect(w.confidence, id).toBeLessThanOrEqual(1);
      }
    }
  });

  it("keeps seed and structure coherent (one canonical cast)", () => {
    // D-36-seed-field: the drafter seed and the structured starter are the same
    // identity, never a divergent second cast.
    for (const { id, name, role, structure: s } of ALL_EXAMPLES) {
      expect(s.identity.name, id).toBe(name);
      expect(s.identity.role, id).toBe(role);
    }
  });

  it("bakes NO premade voice or visual_style (generate-on-create governs)", () => {
    // D-36-X-asset-strategy: avatar + voice are generated on create from the
    // EDITED identity (the shipped async enrichment), so a starter must not pin
    // a premade asset that would mismatch edits. No premade in v1.
    for (const { id, structure: s } of ALL_EXAMPLES) {
      const identity = s.identity as Record<string, unknown>;
      expect(identity.voice, id).toBeUndefined();
      expect(identity.visual_style, id).toBeUndefined();
    }
  });

  it("positions every starter with an aspirational roadmap clause", () => {
    // Criterion 8: each starter conveys phase-3 ambition — as PROSE only, never
    // as wired capability (D-36-roadmap-signalling).
    for (const { id, structure: s } of ALL_EXAMPLES) {
      expect(s.identity.background, id).toContain("Roadmap:");
    }
  });

  it("collectively wires a broad spread of real capability families", () => {
    // The roster's WIRING (not just its prose) must exercise the platform's
    // breadth — proof the showcase is real, not narrated.
    const wiredTools = new Set(ALL_EXAMPLES.flatMap((e) => e.structure.tools));
    const wiredSkills = new Set(
      ALL_EXAMPLES.flatMap((e) => e.structure.skills),
    );
    for (const tool of [
      "web_search",
      "code_execution",
      "render_diagram",
      "generate_image",
      "currency_convert",
      "text_diff",
      "file_write",
      "mcp:github",
      "mcp:weather",
      "mcp:time",
    ]) {
      expect(wiredTools, `no starter wires ${tool}`).toContain(tool);
    }
    for (const skill of SKILL_PALETTE) {
      expect(wiredSkills, `no starter wires skill ${skill}`).toContain(skill);
    }
  });
});
