/**
 * Spec 34 — render test for the branded auth shell + brand panel.
 *
 * Asserts the split-layout chrome: the theme-swapped stacked logo lockups, the
 * brand copy, the four typed-memory store dots, and that the flow form renders
 * in the right-hand panel. Presentation only — no Clerk client involved.
 */
import { render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it } from "vitest";
import messages from "@/i18n/messages/en.json";
import { AuthShell } from "./auth-shell.cloud";

const BRAND = {
  kicker: "Typed-memory AI",
  tagline: "The persona you talk to is the one you type to.",
  note: "Sign in to personas that remember you.",
  compact: "Sign in to personas that remember you.",
} as const;

function renderShell() {
  return render(
    // The store-dot labels come from `auth.stores.*` now.
    <NextIntlClientProvider locale="en" messages={messages}>
      <AuthShell brand={BRAND}>
        <h1>Welcome back</h1>
        <button type="submit">Continue</button>
      </AuthShell>
    </NextIntlClientProvider>,
  );
}

describe("AuthShell", () => {
  it("renders the brand copy", () => {
    renderShell();
    expect(screen.getByText(BRAND.tagline)).toBeTruthy();
    expect(screen.getByText(BRAND.kicker)).toBeTruthy();
  });

  it("renders both theme-swapped logo lockups (one shown per theme)", () => {
    renderShell();
    const lockups = screen.getAllByAltText("Open Persona");
    expect(lockups).toHaveLength(2);
    const sources = lockups.map((el) => el.getAttribute("src") ?? "");
    expect(
      sources.some((src) => src.includes("logo-lockup-stacked-light")),
    ).toBe(true);
    expect(
      sources.some((src) => src.includes("logo-lockup-stacked-dark")),
    ).toBe(true);
  });

  it("renders the four typed-memory store labels", () => {
    renderShell();
    for (const label of ["identity", "self", "worldview", "episodic"]) {
      expect(screen.getByText(label)).toBeTruthy();
    }
  });

  it("renders the flow form passed as children", () => {
    renderShell();
    expect(screen.getByRole("heading", { name: "Welcome back" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Continue" })).toBeTruthy();
  });
});
