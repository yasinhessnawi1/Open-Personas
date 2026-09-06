/**
 * Spec M5 (T6) — the settings page's credit warnings lead somewhere (D-M5-7).
 *
 * Both warnings on this page used to state the problem and stop: "running low" and
 * "exhausted" were rendered on a page with nothing to do about either (§1c.2, §1c.3).
 * Now that `/settings/billing` exists, each carries a link to it.
 *
 * This suite exists because the CTAs live in the PAGE's composition, not in the
 * components. Both `<LowBalanceWarningCard>`'s action and `<ErrorState>`'s action are
 * optional props: drop either one and the components still render happily, the
 * component tests still pass, and the dead end silently returns. Only a test of the
 * page catches that.
 *
 * The DO-NOT-TOUCH plumbing is deliberately exercised rather than stubbed: the same
 * four reads, and the same `credits.balance === 0` branch that decides which warning
 * shows.
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import messages from "@/i18n/messages/en.json";
import SettingsPage from "./page";

const apiGet = vi.fn();

vi.mock("@/auth/server", () => ({
  currentUser: () =>
    Promise.resolve({
      firstName: "Ada",
      lastName: "L",
      primaryEmailAddress: { emailAddress: "ada@example.test" },
    }),
}));

vi.mock("@/lib/api/server", () => ({
  serverApi: () => Promise.resolve({ GET: apiGet }),
}));

vi.mock("next-intl/server", () => ({
  getTranslations: (ns: string) => {
    const table = messages[ns as keyof typeof messages] as Record<
      string,
      string
    >;
    return Promise.resolve((key: string) => table[key] ?? `${ns}.${key}`);
  },
  getFormatter: () =>
    Promise.resolve({
      number: (n: number) => String(n),
      dateTime: (d: Date) => d.toISOString(),
    }),
}));

// A client child that reaches Clerk through `useTheme`/`useBoolSetting`; irrelevant to
// the CTAs under test and covered by its own suite.
vi.mock("@/components/settings/preferences-card", () => ({
  PreferencesCard: () => null,
}));

/** Drives the page's real reads with a given balance. */
function serveBalance(balance: number, lowBalance: boolean) {
  apiGet.mockImplementation((path: string) => {
    if (path === "/v1/me/credits") {
      return Promise.resolve({ data: { balance, low_balance: lowBalance } });
    }
    if (path === "/v1/me/usage") return Promise.resolve({ data: [] });
    return Promise.resolve({ data: [] });
  });
}

async function renderPage() {
  return render(await SettingsPage());
}

function billingLinks(): string[] {
  // The warnings' CTAs only: the page nav now always carries a Billing link
  // (R9-137), which is asserted separately below.
  return Array.from(document.querySelectorAll('a[href="/settings/billing"]'))
    .filter((a) => a.closest("nav") === null)
    .map((a) => a.textContent ?? "");
}

describe("settings page credit warnings", () => {
  it("gives the low-balance warning a way to act on it", async () => {
    serveBalance(500, true);
    await renderPage();

    const action = document.querySelector(
      '[data-slot="settings-low-balance-action"]',
    );
    expect(action).not.toBeNull();
    expect(action?.querySelector("a")?.getAttribute("href")).toBe(
      "/settings/billing",
    );
  });

  it("gives the exhausted cliff a way out", async () => {
    // The 402 is the hard stop: nothing works until credits exist, so this is where a
    // dead end costs the most. ErrorState has had the action slot since T22; it was
    // simply never filled.
    serveBalance(0, true);
    await renderPage();

    const errorState = document.querySelector('[data-slot="error-state"]');
    expect(errorState?.getAttribute("data-status")).toBe("402");
    const action = document.querySelector('[data-slot="error-state-action"]');
    expect(action?.querySelector("a")?.getAttribute("href")).toBe(
      "/settings/billing",
    );
  });

  it("shows the exhausted state INSTEAD of the low-balance card at zero", async () => {
    // The DO-NOT-TOUCH zero-guard still decides which warning appears. Two warnings at
    // once would be noise, and none would be the original bug.
    serveBalance(0, true);
    await renderPage();

    expect(document.querySelector('[data-slot="error-state"]')).not.toBeNull();
    expect(
      document.querySelector('[data-slot="settings-low-balance-warning"]'),
    ).toBeNull();
  });

  it("shows no warning at all on a healthy balance", async () => {
    // The CTAs must not leak onto a page where nothing is wrong.
    serveBalance(5000, false);
    await renderPage();

    expect(
      document.querySelector('[data-slot="settings-low-balance-warning"]'),
    ).toBeNull();
    expect(document.querySelector('[data-slot="error-state"]')).toBeNull();
    expect(billingLinks()).toEqual([]);
  });

  it("labels the CTA with real copy, not a raw key", async () => {
    serveBalance(500, true);
    await renderPage();
    expect(screen.getByText(messages.settings.addCredits)).toBeTruthy();
    expect(billingLinks().join()).not.toContain("settings.");
  });
});

describe("billing entry point (R9-137)", () => {
  it("offers Billing from the page nav even with a healthy wallet", async () => {
    serveBalance(500, false);
    await renderPage();
    // Healthy wallet: no low-balance card, no 402 cliff, so the warnings offer
    // nothing; the nav must still lead to billing. Before this it did not.
    expect(billingLinks()).toEqual([]);
    const nav = document.querySelector('nav a[href="/settings/billing"]');
    expect(nav).not.toBeNull();
    expect(nav?.textContent).toBe("Billing");
  });
});
