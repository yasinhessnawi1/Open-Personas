import { describe, expect, it } from "vitest";
import {
  docToYaml,
  type PersonaDoc,
  readIdentity,
  readPreferredModel,
  readSelfFacts,
  readWorldview,
  writeIdentityField,
  writePreferredModel,
  writeSelfFacts,
  writeStringList,
  writeWorldview,
  yamlToDoc,
} from "./persona-draft";

const SAMPLE = `schema_version: "1.0"
identity:
  name: Astrid
  role: Tenancy assistant
  background: Helps tenants.
  language_default: en
  constraints:
    - Never give binding advice.
self_facts:
  - fact: Specialised in tenancy.
    confidence: 1
worldview:
  - claim: Tenants have rights.
    domain: tenancy
    epistemic: fact
    confidence: 0.95
    valid_time: always
tools:
  - web_search
skills: []
routing:
  tier_for_generation: auto
embedding:
  model: bge-small-en-v1.5
`;

describe("persona-draft round-trip", () => {
  it("parses YAML and reads structured fields", () => {
    const doc = yamlToDoc(SAMPLE);
    const id = readIdentity(doc);
    expect(id.name).toBe("Astrid");
    expect(id.constraints).toEqual(["Never give binding advice."]);
    expect(readSelfFacts(doc)[0]).toEqual({
      fact: "Specialised in tenancy.",
      confidence: 1,
    });
    expect(readWorldview(doc)[0].epistemic).toBe("fact");
  });

  it("survives a doc → yaml → doc round-trip", () => {
    const doc = yamlToDoc(SAMPLE);
    const round = yamlToDoc(docToYaml(doc));
    expect(round).toEqual(doc);
  });

  it("throws on invalid YAML and on a non-mapping top level", () => {
    expect(() => yamlToDoc("identity: : :")).toThrow();
    expect(() => yamlToDoc("- just\n- a\n- list")).toThrow();
  });
});

describe("persona-draft writers preserve sibling keys", () => {
  it("writeIdentityField keeps routing/embedding and other identity fields", () => {
    const doc = yamlToDoc(SAMPLE);
    const next = writeIdentityField(doc, "name", "Bjorn");
    expect(readIdentity(next).name).toBe("Bjorn");
    expect(readIdentity(next).role).toBe("Tenancy assistant"); // sibling kept
    expect((next as PersonaDoc).routing).toEqual(doc.routing); // top-level kept
    expect((next as PersonaDoc).embedding).toEqual(doc.embedding);
  });

  it("writeSelfFacts / writeWorldview / writeStringList replace only their slice", () => {
    const doc = yamlToDoc(SAMPLE);
    const a = writeSelfFacts(doc, [{ fact: "New fact.", confidence: 0.5 }]);
    expect(readSelfFacts(a)).toHaveLength(1);
    expect(readSelfFacts(a)[0].fact).toBe("New fact.");
    expect(a.routing).toEqual(doc.routing);

    const b = writeWorldview(doc, []);
    expect(readWorldview(b)).toEqual([]);

    const c = writeStringList(doc, "tools", ["web_search", "web_fetch"]);
    expect(c.tools).toEqual(["web_search", "web_fetch"]);
    expect(c.skills).toEqual([]);
  });

  it("invalid YAML in the editor leaves the prior doc usable (sync invariant)", () => {
    // Mirrors the editor's behaviour: a parse failure must not lose the form.
    const lastValid = yamlToDoc(SAMPLE);
    let doc = lastValid;
    try {
      doc = yamlToDoc("identity: : :");
    } catch {
      // keep last valid
    }
    expect(doc).toBe(lastValid);
  });
});

describe("stored routing blocks survive untouched (Spec P9, P9-D-5 back-compat)", () => {
  // The tuning surface is retired: persona-draft no longer reads or writes
  // `routing.*`. Criterion 5's load-bearing half — an old persona carrying a
  // pinned tier (a deliberate override, P9-D-7) must load byte-identically and
  // keep its pin through every editor write path, so the server keeps honoring
  // it. (That the pin still ROUTES is proven server-side in
  // test_loop_policy_routing.py / test_reply_producer_routing.py.)
  const PINNED = `schema_version: "1.0"
identity:
  name: Old-Timer
  role: Legacy persona
  background: Authored before P9.
  constraints:
    - Keep the pin.
routing:
  tier_for_generation: mid
  tier_for_tools: small
  intelligent:
    enabled: true
    weights: { cost: 0.7, quality: 0.25, latency: 0.05 }
  budget:
    max_cents_per_turn: 2.5
`;

  it("a pinned persona round-trips byte-identically (load + save changes nothing)", () => {
    const doc = yamlToDoc(PINNED);
    expect(yamlToDoc(docToYaml(doc))).toEqual(doc);
    const routing = doc.routing as Record<string, unknown>;
    expect(routing.tier_for_generation).toBe("mid");
  });

  it("every editor write path preserves the stored routing block verbatim", () => {
    const doc = yamlToDoc(PINNED);
    const afterIdentity = writeIdentityField(doc, "name", "Renamed");
    const afterFacts = writeSelfFacts(afterIdentity, [
      { fact: "New fact.", confidence: 0.5 },
    ]);
    const afterTools = writeStringList(afterFacts, "tools", ["web_search"]);
    const afterWorldview = writeWorldview(afterTools, []);
    expect(afterWorldview.routing).toEqual(doc.routing);
  });

  it("the editor never authors a routing block into a fresh draft", () => {
    const doc = yamlToDoc(SAMPLE);
    const edited = writeIdentityField(doc, "name", "Bjorn");
    // SAMPLE carries only the legacy `tier_for_generation: auto` — no writer
    // adds intelligent/budget config the user never asked for.
    expect(edited.routing).toEqual(doc.routing);
    expect(
      (edited.routing as Record<string, unknown>).intelligent,
    ).toBeUndefined();
  });
});

describe("preferred_model reader/writer (Spec M1, M1-T7): additive, preserves routing siblings", () => {
  it("reads null when routing (or preferred_model) is absent", () => {
    expect(readPreferredModel(yamlToDoc(SAMPLE))).toBeNull();
    expect(readPreferredModel({})).toBeNull();
  });

  it("writes a fresh routing block on a doc with none", () => {
    const doc: PersonaDoc = { schema_version: "1.0" };
    const next = writePreferredModel(doc, "anthropic/claude-sonnet-4.6");
    expect(readPreferredModel(next)).toBe("anthropic/claude-sonnet-4.6");
  });

  it("preserves sibling routing keys (a tier pin) when setting then clearing the model", () => {
    const doc = yamlToDoc(SAMPLE); // routing: { tier_for_generation: auto }
    const withModel = writePreferredModel(doc, "z-ai/glm-4.6");
    expect(readPreferredModel(withModel)).toBe("z-ai/glm-4.6");
    expect(
      (withModel.routing as Record<string, unknown>).tier_for_generation,
    ).toBe("auto");

    const cleared = writePreferredModel(withModel, null);
    expect(readPreferredModel(cleared)).toBeNull();
    expect(
      (cleared.routing as Record<string, unknown>).tier_for_generation,
    ).toBe("auto");
  });

  it("clearing the only routing key drops the routing block entirely (no stray routing: {})", () => {
    const doc: PersonaDoc = {
      schema_version: "1.0",
      routing: { preferred_model: "x/y" },
    };
    const cleared = writePreferredModel(doc, null);
    expect(cleared.routing).toBeUndefined();
  });

  it("clearing an already-unset model on a pinned legacy block is a byte-identical no-op", () => {
    const doc: PersonaDoc = {
      schema_version: "1.0",
      routing: { tier_for_generation: "mid", intelligent: { enabled: true } },
    };
    expect(writePreferredModel(doc, null)).toEqual(doc);
  });

  it("round-trips through YAML", () => {
    const doc = writePreferredModel(yamlToDoc(SAMPLE), "openai/gpt-5.1");
    const round = yamlToDoc(docToYaml(doc));
    expect(readPreferredModel(round)).toBe("openai/gpt-5.1");
  });
});
