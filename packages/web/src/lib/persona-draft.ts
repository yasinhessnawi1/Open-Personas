import yaml from "js-yaml";

// Editable persona-document model for the authoring + edit flows (T08, D-09-9).
// The parsed object (`PersonaDoc`) is the single source of truth; the form edits
// known fields while preserving any unknown keys (routing, embedding, visibility,
// persona_id …) by spreading. Invalid YAML keeps the last valid doc (the editor
// surfaces the error). Mirrors the v1.0 schema in
// packages/core/src/persona/schema/persona.py.

export type PersonaDoc = Record<string, unknown>;

export const EPISTEMIC_OPTIONS = [
  "fact",
  "belief",
  "hypothesis",
  "contested",
] as const;

export interface IdentityView {
  name: string;
  role: string;
  background: string;
  language_default: string;
  constraints: string[];
}

export interface SelfFactView {
  fact: string;
  confidence: number;
}

export interface WorldviewView {
  claim: string;
  domain: string;
  epistemic: string;
  confidence: number;
  valid_time: string;
}

function asRecord(v: unknown): Record<string, unknown> {
  return v && typeof v === "object" && !Array.isArray(v)
    ? (v as Record<string, unknown>)
    : {};
}

function asString(v: unknown, fallback = ""): string {
  return typeof v === "string" ? v : fallback;
}

function asNumber(v: unknown, fallback: number): number {
  return typeof v === "number" && Number.isFinite(v) ? v : fallback;
}

function asStringList(v: unknown): string[] {
  return Array.isArray(v)
    ? v.filter((x): x is string => typeof x === "string")
    : [];
}

/** A minimal valid-shaped v1.0 skeleton (fields are filled by the user/author). */
export function emptyPersonaDoc(): PersonaDoc {
  return {
    schema_version: "1.0",
    identity: {
      name: "",
      role: "",
      background: "",
      language_default: "en",
      constraints: [],
    },
    self_facts: [],
    worldview: [],
    tools: [],
    skills: [],
  };
}

/** Serialise a doc to YAML (block style, no wrapping of long background text). */
export function docToYaml(doc: PersonaDoc): string {
  return yaml.dump(doc, { lineWidth: -1, noRefs: true, sortKeys: false });
}

/** Parse YAML into a doc; throws on invalid YAML or a non-mapping top level. */
export function yamlToDoc(src: string): PersonaDoc {
  const parsed = yaml.load(src);
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("Persona YAML must be a mapping at the top level.");
  }
  return parsed as PersonaDoc;
}

// ----- typed readers -----

export function readIdentity(doc: PersonaDoc): IdentityView {
  const id = asRecord(doc.identity);
  return {
    name: asString(id.name),
    role: asString(id.role),
    background: asString(id.background),
    language_default: asString(id.language_default, "en"),
    constraints: asStringList(id.constraints),
  };
}

export function readSelfFacts(doc: PersonaDoc): SelfFactView[] {
  const list = Array.isArray(doc.self_facts) ? doc.self_facts : [];
  return list.map((f) => {
    const r = asRecord(f);
    return { fact: asString(r.fact), confidence: asNumber(r.confidence, 1) };
  });
}

export function readWorldview(doc: PersonaDoc): WorldviewView[] {
  const list = Array.isArray(doc.worldview) ? doc.worldview : [];
  return list.map((w) => {
    const r = asRecord(w);
    return {
      claim: asString(r.claim),
      domain: asString(r.domain),
      epistemic: asString(r.epistemic, "belief"),
      confidence: asNumber(r.confidence, 0.8),
      valid_time: asString(r.valid_time, "always"),
    };
  });
}

export function readStringList(doc: PersonaDoc, key: string): string[] {
  return asStringList(doc[key]);
}

// ----- immutable writers (return a new doc, preserving sibling keys) -----

export function writeIdentityField(
  doc: PersonaDoc,
  key: keyof IdentityView,
  value: string | string[],
): PersonaDoc {
  return { ...doc, identity: { ...asRecord(doc.identity), [key]: value } };
}

export function writeSelfFacts(
  doc: PersonaDoc,
  facts: SelfFactView[],
): PersonaDoc {
  return { ...doc, self_facts: facts };
}

export function writeWorldview(
  doc: PersonaDoc,
  claims: WorldviewView[],
): PersonaDoc {
  return { ...doc, worldview: claims };
}

export function writeStringList(
  doc: PersonaDoc,
  key: string,
  list: string[],
): PersonaDoc {
  return { ...doc, [key]: list };
}

// ----- routing (Spec P9, P9-D-5): the Spec-31 tuning surface is retired -----
//
// The persona doc's `routing:` block (tier pins, intelligent/budget config) is
// deliberately UNREAD and UNWRITTEN here: surface→tier is the product policy
// (packages/runtime routing/policy.py), not a per-persona tuning surface. A
// stored block survives every writer above untouched (spread-preserved), so an
// old persona with a pinned tier loads byte-identically and still routes by
// its pin server-side.
