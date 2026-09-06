/**
 * Spec M5 (T1) — the billing route + capability gate.
 *
 * The load-bearing assertions: the page RENDERS for the Stripe return URLs prod
 * already points at (the live 404 this task closes); the capability gate shows no
 * purchase affordances when `/v1/billing/config` 404s (community / flag-off,
 * D-M5-3); and the `?checkout=success` landing never claims credits were added,
 * because the webhook may not have landed yet (D-M5-2 / the R9-050 honest-loading
 * rule).
 */

import { render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it, vi } from "vitest";
import messages from "@/i18n/messages/en.json";
import BillingPage from "./page";

const configGet = vi.fn();

vi.mock("@/lib/api/server", () => ({
  serverApi: () =>
    Promise.resolve({
      // T2c added a second server-side read (`/v1/me/wallet`). This suite is about the
      // ROUTE + capability gate, so only the config probe is driven here; the wallet
      // resolves empty and its own behaviour is covered by billing-wallet.test.tsx /
      // subscription-status-card.test.tsx.
      GET: (path: string) =>
        path === "/v1/billing/config"
          ? configGet(path)
          : Promise.resolve({ data: null }),
    }),
}));

// The billing page composes client components that call `useApi()` → Clerk's `useAuth`,
// which throws outside a <ClerkProvider>. Stubbed here so this suite stays a test of the
// page's own composition + capability gate; each is exercised in its own file and, more
// to the point, in a real browser (the T2c/T3 community + cloud passes).
//
// NB: the disabled-path tests below (which assert ZERO buttons) still render the REAL
// page, because neither child is mounted when billing is off — so the capability gate is
// not hollowed out by these stubs.
vi.mock("@/components/settings/billing-wallet", () => ({
  BillingWallet: () => null,
}));
vi.mock("@/components/settings/billing-plans", () => ({
  BillingPlans: () => null,
}));

vi.mock("next-intl/server", () => ({
  getTranslations: (ns: string) => {
    const table = messages[ns as keyof typeof messages] as Record<
      string,
      string
    >;
    return Promise.resolve((key: string) => table[key] ?? `${ns}.${key}`);
  },
}));

/** Renders the async Server Component by awaiting its element. */
async function renderPage(checkout?: string) {
  const ui = await BillingPage({
    searchParams: Promise.resolve(checkout ? { checkout } : {}),
  });
  return render(
    <NextIntlClientProvider locale="en" messages={messages}>
      {ui}
    </NextIntlClientProvider>,
  );
}

/** Billing enabled (cloud + flag + key). */
function billingEnabled() {
  configGet.mockResolvedValue({ data: { enabled: true } });
}

/** The community / flag-off answer: the whole `/v1/billing` surface 404s. */
function billingDisabled() {
  configGet.mockRejectedValue(
    Object.assign(new Error("API 404 (not_found)"), { status: 404 }),
  );
}

describe("billing route", () => {
  it("renders the billing page instead of a 404", async () => {
    billingEnabled();
    await renderPage();
    expect(screen.getByText(messages.billing.title)).toBeTruthy();
  });

  it("renders the Stripe success return URL prod already points at", async () => {
    billingEnabled();
    await renderPage("success");
    expect(
      document.querySelector('[data-slot="checkout-success"]'),
    ).not.toBeNull();
  });

  it("renders the Stripe cancel return URL", async () => {
    billingEnabled();
    await renderPage("cancel");
    expect(
      document.querySelector('[data-slot="checkout-cancel"]'),
    ).not.toBeNull();
  });

  it("does not claim credits were added before the webhook lands", async () => {
    billingEnabled();
    await renderPage("success");
    const body = messages.billing.successBody.toLowerCase();
    // Honest: confirms the PAYMENT, promises the balance follows. It must never
    // assert a settled balance or render a fabricated number (D-M5-2).
    expect(body).not.toMatch(/credits added|added to your balance/);
    expect(screen.getByText(messages.billing.successTitle)).toBeTruthy();
    expect(/\d/.test(messages.billing.successBody)).toBe(false);
  });

  it("shows no checkout landing card on a plain visit", async () => {
    billingEnabled();
    await renderPage();
    expect(document.querySelector('[data-slot="checkout-success"]')).toBeNull();
    expect(document.querySelector('[data-slot="checkout-cancel"]')).toBeNull();
  });

  it("degrades to the unmetered state when billing is disabled", async () => {
    billingDisabled();
    await renderPage();
    expect(screen.getByText(messages.billing.unmeteredBody)).toBeTruthy();
    expect(screen.queryByText(messages.billing.enabledIntro)).toBeNull();
  });

  it("offers no purchase affordance when billing is disabled", async () => {
    billingDisabled();
    const { container } = await renderPage();
    // The capability gate's whole point: no dead buttons, no "upgrade" that 404s.
    expect(container.querySelectorAll("button").length).toBe(0);
  });
});
