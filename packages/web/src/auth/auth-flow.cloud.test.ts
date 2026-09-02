/**
 * Spec 34 — unit tests for the framework-free branded-auth logic.
 *
 * Covers the pieces that don't need a Clerk-bound render: the OAuth provider
 * gate (must ship empty/OFF for v1), the Clerk-error -> themed-message mapper
 * (incl. lockout copy), and the cooldown formatter. The hook-driven flows are
 * exercised by the user's real-browser pass.
 */
import { describe, expect, it } from "vitest";
import messages from "@/i18n/messages/en.json";
import {
  clerkErrorToMessage,
  dedupeFieldError,
  formatCooldown,
  isLockoutError,
  OAUTH_PROVIDERS,
  OAUTH_PROVIDERS_ALL,
  RESEND_COOLDOWN_SECONDS,
} from "./auth-flow.cloud";

// The fallback copy lives in `auth.error.*` now; the mapper takes a resolver so
// it stays pure. This stub is what next-intl hands it at runtime.
const t = (key: "generic" | "lockout") => messages.auth.error[key];
const GENERIC_ERROR_MESSAGE = messages.auth.error.generic;
const LOCKOUT_MESSAGE = messages.auth.error.lockout;

describe("OAuth provider gate (D-34-3)", () => {
  it("ships OFF by default — no dead provider button reaches users", () => {
    expect(OAUTH_PROVIDERS).toHaveLength(0);
  });

  it("keeps the full Google + GitHub definitions ready to enable", () => {
    expect(OAUTH_PROVIDERS_ALL.map((p) => p.strategy)).toEqual([
      "oauth_google",
      "oauth_github",
    ]);
    for (const provider of OAUTH_PROVIDERS_ALL) {
      expect(provider.strategy.startsWith("oauth_")).toBe(true);
      expect(messages.auth.oauth[provider.labelKey].length).toBeGreaterThan(0);
    }
  });
});

describe("clerkErrorToMessage", () => {
  it("falls back to the generic message for a null/undefined error", () => {
    expect(clerkErrorToMessage(null, t)).toBe(GENERIC_ERROR_MESSAGE);
    expect(clerkErrorToMessage(undefined, t)).toBe(GENERIC_ERROR_MESSAGE);
  });

  it("prefers longMessage over message", () => {
    expect(
      clerkErrorToMessage(
        {
          code: "form_password_incorrect",
          message: "Password is incorrect.",
          longMessage: "That password is incorrect. Try again.",
        },
        t,
      ),
    ).toBe("That password is incorrect. Try again.");
  });

  it("falls back to message when longMessage is absent/blank", () => {
    expect(
      clerkErrorToMessage({ code: "x", message: "Something specific." }, t),
    ).toBe("Something specific.");
    expect(
      clerkErrorToMessage(
        { code: "x", message: "Only this.", longMessage: "   " },
        t,
      ),
    ).toBe("Only this.");
  });

  it("uses the calm lockout copy for rate-limit / locked codes", () => {
    for (const code of ["too_many_requests", "user_locked", "account_locked"]) {
      expect(
        clerkErrorToMessage({ code, message: "raw provider string" }, t),
      ).toBe(LOCKOUT_MESSAGE);
    }
  });

  it("never surfaces a raw empty string", () => {
    expect(
      clerkErrorToMessage({ code: "z", message: "", longMessage: "" }, t),
    ).toBe(GENERIC_ERROR_MESSAGE);
  });
});

describe("isLockoutError", () => {
  it("is true only for lockout / rate-limit codes", () => {
    expect(isLockoutError({ code: "too_many_requests" })).toBe(true);
    expect(isLockoutError({ code: "form_password_incorrect" })).toBe(false);
    expect(isLockoutError(null)).toBe(false);
    expect(isLockoutError({})).toBe(false);
  });
});

describe("dedupeFieldError", () => {
  it("suppresses a field error equal to the banner (no double render)", () => {
    expect(
      dedupeFieldError(
        "Couldn't find your account.",
        "Couldn't find your account.",
      ),
    ).toBeNull();
  });

  it("ignores surrounding whitespace when comparing", () => {
    expect(
      dedupeFieldError(
        "  Couldn't find your account.  ",
        "Couldn't find your account.",
      ),
    ).toBeNull();
  });

  it("keeps a genuinely field-specific error (different from the banner)", () => {
    expect(
      dedupeFieldError("Your password must be at least 8 characters.", null),
    ).toBe("Your password must be at least 8 characters.");
    expect(
      dedupeFieldError(
        "Your password must be at least 8 characters.",
        "Couldn't find your account.",
      ),
    ).toBe("Your password must be at least 8 characters.");
  });

  it("returns null for an empty/absent field message", () => {
    expect(dedupeFieldError(null, "anything")).toBeNull();
    expect(dedupeFieldError(undefined, null)).toBeNull();
    expect(dedupeFieldError("   ", null)).toBeNull();
  });
});

describe("formatCooldown", () => {
  it("formats a positive remaining count as 'Ns'", () => {
    expect(formatCooldown(RESEND_COOLDOWN_SECONDS)).toBe("30s");
    expect(formatCooldown(29)).toBe("29s");
    expect(formatCooldown(1)).toBe("1s");
  });

  it("rounds up fractional seconds (a mid-tick value still reads whole)", () => {
    expect(formatCooldown(4.2)).toBe("5s");
  });

  it("returns an empty string once the cooldown has elapsed", () => {
    expect(formatCooldown(0)).toBe("");
    expect(formatCooldown(-3)).toBe("");
    expect(formatCooldown(Number.NaN)).toBe("");
  });
});
