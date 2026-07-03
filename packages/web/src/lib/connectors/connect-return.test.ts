import { describe, expect, it } from "vitest";
import { resolveReturnToast } from "./connect-return";

/**
 * Spec C6 (T7) — the OAuth return voice keys off the LIST, never the raw `?result=` param
 * (C6-D-2). The load-bearing case: a claimed `connected` with NO binding present is an honest
 * "unconfirmed", never a success — so a forged/stale success can't produce a connected voice.
 */
describe("resolveReturnToast", () => {
  it("connected + a real binding → success", () => {
    expect(resolveReturnToast("connected", true)).toBe("success");
  });

  it("connected but NO binding → unconfirmed (never success — the adversarial case)", () => {
    expect(resolveReturnToast("connected", false)).toBe("unconfirmed");
  });

  it("denied / expired / failed map to their honest voice regardless of the list", () => {
    expect(resolveReturnToast("denied", false)).toBe("denied");
    expect(resolveReturnToast("expired", false)).toBe("expired");
    expect(resolveReturnToast("failed", true)).toBe("failed");
  });

  it("an unknown or absent result produces no voice", () => {
    expect(resolveReturnToast(null, true)).toBeNull();
    expect(resolveReturnToast("", false)).toBeNull();
    expect(resolveReturnToast("bogus", true)).toBeNull();
  });
});
