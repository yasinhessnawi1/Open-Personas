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

// The edition the build selected. Switched per test; cloud unless a test says otherwise.
const build = vi.hoisted(() => ({ edition: "cloud" as "cloud" | "community" }));

vi.mock("@/auth/server", () => ({
  get EDITION() {
    return build.edition;
  },
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

/**
 * Drives the page's real reads with a given balance. Billing is on (cloud) by default;
 * "404" answers the config read as community does, "disabled" as a metered install
 * with billing switched off does.
 */
function serveBalance(
  balance: number,
  lowBalance: boolean,
  billing: "on" | "404" | "disabled" = "on",
) {
  apiGet.mockImplementation((path: string) => {
    if (path === "/v1/me/credits") {
      return Promise.resolve({ data: { balance, low_balance: lowBalance } });
    }
    if (path === "/v1/billing/config") {
      return billing === "404"
        ? Promise.reject(new Error("404"))
        : Promise.resolve({ data: { enabled: billing === "on" } });
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
  it("shows the balance in dollars under a Balance heading", async () => {
    // R9-177 B6 (owner ruling 2026-09-26): every wallet balance speaks dollars.
    serveBalance(123_450, false);
    await renderPage();
    const card = document.querySelector('[data-slot="settings-credits"]');
    expect(card?.querySelector("h2")?.textContent).toBe("Balance");
    expect(
      document.querySelector('[data-slot="settings-credits-balance"]')
        ?.textContent,
    ).toBe("$1,234.50");
  });

  it.each(["disabled", "404"] as const)(
    "still shows dollars on a metered install whatever the billing config says (%s)",
    async (billing) => {
      // Review MEDIUM-1: a cloud install with Stripe billing off, or a config read that
      // fails (a 500, a 401 race), still meters. Its balance is money, not "Unlimited".
      serveBalance(2350, false, billing);
      await renderPage();
      expect(
        document.querySelector('[data-slot="settings-credits-balance"]')
          ?.textContent,
      ).toBe("$23.50");
      expect(
        document.querySelector('[data-slot="settings-credits-hint"]')
          ?.textContent,
      ).toBe("Your balance is spent per turn, by model tier.");
    },
  );

  it("shows Unlimited, never a dollar figure or a spending hint, on community", async () => {
    // Community is unmetered and reports a sentinel balance of a billion credits. As
    // money that read "$10,000,000"; it is not money, so it must not look like it.
    build.edition = "community";
    try {
      serveBalance(1_000_000_000, false, "404");
      await renderPage();
      const card = document.querySelector('[data-slot="settings-credits"]');
      expect(
        card?.querySelector('[data-slot="settings-credits-balance"]')
          ?.textContent,
      ).toBe("Unlimited");
      expect(card?.textContent).not.toMatch(/\$/);
      expect(
        card?.querySelector('[data-slot="settings-credits-hint"]'),
      ).toBeNull();
    } finally {
      build.edition = "cloud";
    }
  });

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
