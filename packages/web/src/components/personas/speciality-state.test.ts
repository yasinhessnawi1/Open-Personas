import { describe, expect, it } from "vitest";
import {
  deriveSpecialityState,
  isSpecialityEnabled,
  needsConsent,
  type SpecialityEntry,
} from "./speciality-state";

function spec(over: Partial<SpecialityEntry> = {}): SpecialityEntry {
  return {
    name: "legal_research",
    description: "Legal research helper.",
    when_to_use: null,
    trust: "third_party",
    requires_consent: true,
    content_hash: "hash_v1",
    source: "github:acme/skills",
    source_uri: null,
    source_ref: null,
    consent_state: "none",
    ...over,
  };
}

describe("deriveSpecialityState (S3-D-4)", () => {
  it("is available when not declared", () => {
    expect(deriveSpecialityState(spec(), [])).toBe("available");
  });

  it("is enabled for a declared non-gated (builtin) speciality", () => {
    const s = spec({
      name: "code_review",
      trust: "builtin",
      requires_consent: false,
      consent_state: "not_required",
    });
    expect(deriveSpecialityState(s, ["code_review"])).toBe("enabled");
  });

  it("is enabled for a declared gated speciality consented at the current hash", () => {
    const s = spec({ consent_state: "granted" });
    expect(deriveSpecialityState(s, ["legal_research"])).toBe("enabled");
  });

  it("is needs-consent when declared but never consented (default-deny)", () => {
    const s = spec({ consent_state: "none" });
    expect(deriveSpecialityState(s, ["legal_research"])).toBe("needs-consent");
  });

  it("is needs-consent when declared but consent is stale (body changed, S1-D-5)", () => {
    const s = spec({ consent_state: "stale" });
    expect(deriveSpecialityState(s, ["legal_research"])).toBe("needs-consent");
  });

  it("is available (not needs-consent) for a gated speciality that is not declared", () => {
    expect(deriveSpecialityState(spec({ consent_state: "none" }), [])).toBe(
      "available",
    );
  });

  it("is unavailable when declared-then-dropped, overriding enabled", () => {
    const s = spec({
      trust: "builtin",
      requires_consent: false,
      consent_state: "not_required",
    });
    expect(
      deriveSpecialityState(s, ["legal_research"], ["legal_research"]),
    ).toBe("unavailable");
  });
});

describe("isSpecialityEnabled / needsConsent", () => {
  it("isSpecialityEnabled checks membership in the declared skills list", () => {
    expect(isSpecialityEnabled("legal_research", ["legal_research"])).toBe(
      true,
    );
    expect(isSpecialityEnabled("legal_research", ["web_research"])).toBe(false);
  });

  it("needsConsent is true only for a gated tier not consented at the current hash", () => {
    expect(needsConsent(spec({ consent_state: "none" }))).toBe(true);
    expect(needsConsent(spec({ consent_state: "stale" }))).toBe(true);
    expect(needsConsent(spec({ consent_state: "granted" }))).toBe(false);
    expect(
      needsConsent(
        spec({ requires_consent: false, consent_state: "not_required" }),
      ),
    ).toBe(false);
  });
});
