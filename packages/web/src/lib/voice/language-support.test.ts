import { describe, expect, it } from "vitest";
import { unservedVoiceLanguage } from "./language-support";

describe("unservedVoiceLanguage (Spec 32 D author-time hint)", () => {
  it("is silent for served languages and their variants", () => {
    expect(unservedVoiceLanguage("en")).toBeNull();
    expect(unservedVoiceLanguage("no")).toBeNull();
    expect(unservedVoiceLanguage("nb")).toBeNull(); // collapses to no
    expect(unservedVoiceLanguage("nn-NO")).toBeNull();
    expect(unservedVoiceLanguage("en-US")).toBeNull();
    expect(unservedVoiceLanguage("de-CH")).toBeNull(); // base-code fallback
    expect(unservedVoiceLanguage("ar")).toBeNull();
  });

  it("is silent while the field is blank (don't nag mid-typing)", () => {
    expect(unservedVoiceLanguage("")).toBeNull();
    expect(unservedVoiceLanguage("   ")).toBeNull();
  });

  it("names the language when the providers can't serve it", () => {
    // The sentence itself lives in `author.voiceLanguageWarning`; this returns
    // only the value the caller interpolates.
    expect(unservedVoiceLanguage("klingon")).toBe("klingon");
    expect(unservedVoiceLanguage("  klingon  ")).toBe("klingon");
  });
});
