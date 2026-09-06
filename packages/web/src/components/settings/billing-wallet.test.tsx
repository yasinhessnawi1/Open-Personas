/**
 * Spec M5 (T2c) — the wallet on the billing page + the post-checkout re-poll.
 *
 * The load-bearing guarantee is D-M5-2 / R9-050 honesty: on the `?checkout=success`
 * return the grant happens in the WEBHOOK, which may not have landed when Stripe
 * redirects. So the page must never assert a settled balance it has not observed.
 *
 * These tests drive the re-poll through the REAL return path — the component mounted
 * with `awaitingGrant` exactly as the `?checkout=success` page mounts it — and let the
 * served balance change underneath, rather than hand-invoking a refresh. The transition
 * out of "pending" has to be caused by the same chain production uses.
 */

import { render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it, vi } from "vitest";
import messages from "@/i18n/messages/en.json";
import { BillingWallet } from "./billing-wallet";

const walletGet = vi.fn();

vi.mock("@/lib/api/use-api", () => ({
  useApi: () => ({ GET: walletGet }),
}));

/** An openapi-fetch success result, the shape `unwrap` actually consumes. */
function wallet(overrides: Record<string, unknown> = {}) {
  return {
    response: new Response(null, { status: 200 }),
    data: {
      total_balance: 500,
      allowance_balance: 300,
      allowance_period: "2026-08",
      payg_lots: [],
      plan_code: "free",
      auto_topup_enabled: false,
      low_balance: false,
      low_balance_threshold: 60,
      current_period_end: null,
      cancel_at_period_end: false,
      subscription_status: "active",
      ...overrides,
    },
  };
}

function renderWallet(props: Record<string, unknown> = {}) {
  return render(
    <NextIntlClientProvider locale="en" messages={messages}>
      <BillingWallet pollIntervalMs={5} {...props} />
    </NextIntlClientProvider>,
  );
}

afterEach(() => {
  walletGet.mockReset();
});

describe("billing wallet", () => {
  it("shows the served balance", async () => {
    walletGet.mockResolvedValue(wallet());
    renderWallet();
    await waitFor(() =>
      expect(
        document.querySelector('[data-slot="billing-wallet-balance"]')
          ?.textContent,
      ).toBe("500"),
    );
  });

  it("reads low_balance from the API rather than recomputing it", async () => {
    // D-M5-11: the per-plan 20% rule lives server-side. A client-side comparison would
    // drift the moment the rule changes, so the flag must be obeyed even when it
    // contradicts what a naive local threshold check would conclude.
    walletGet.mockResolvedValue(
      wallet({
        total_balance: 9999,
        low_balance: true,
        low_balance_threshold: 60,
      }),
    );
    renderWallet();
    await waitFor(() =>
      expect(
        document.querySelector('[data-slot="billing-wallet-low"]'),
      ).not.toBeNull(),
    );
  });

  it("does not show the low-balance line at a zero balance", async () => {
    // Zero is exhaustion, a different surface (the 402 state), not "running low".
    walletGet.mockResolvedValue(
      wallet({ total_balance: 0, low_balance: true }),
    );
    renderWallet();
    await waitFor(() =>
      expect(
        document.querySelector('[data-slot="billing-wallet"]'),
      ).not.toBeNull(),
    );
    expect(
      document.querySelector('[data-slot="billing-wallet-low"]'),
    ).toBeNull();
  });

  it("degrades honestly when the wallet cannot be read", async () => {
    walletGet.mockRejectedValue(new Error("boom"));
    renderWallet();
    await waitFor(() =>
      expect(screen.getByText(messages.billing.walletUnavailable)).toBeTruthy(),
    );
  });

  // --- the post-checkout re-poll (D-M5-2) ---

  it("says credits are on the way before the webhook lands", async () => {
    walletGet.mockResolvedValue(wallet({ total_balance: 500 }));
    renderWallet({ awaitingGrant: true });
    await waitFor(() =>
      expect(
        document.querySelector('[data-slot="billing-wallet-pending"]'),
      ).not.toBeNull(),
    );
    // The honest part: it reports the balance it actually observed, not an optimistic
    // one that assumes the purchased credits already landed.
    expect(
      document.querySelector('[data-slot="billing-wallet-balance"]')
        ?.textContent,
    ).toBe("500");
  });

  it("clears the pending state when the balance actually moves", async () => {
    // The REAL transition: the served balance changes underneath the poll, exactly as it
    // does when the webhook grants the lot. Nothing here hand-invokes a refresh.
    walletGet
      .mockResolvedValueOnce(wallet({ total_balance: 500 }))
      .mockResolvedValue(wallet({ total_balance: 1500 }));
    renderWallet({ awaitingGrant: true });

    await waitFor(() =>
      expect(
        document.querySelector('[data-slot="billing-wallet-balance"]')
          ?.textContent,
      ).toBe("1500"),
    );
    await waitFor(() =>
      expect(
        document.querySelector('[data-slot="billing-wallet-pending"]'),
      ).toBeNull(),
    );
  });

  it("gives up honestly if the grant never lands", async () => {
    // A webhook that never arrives must not spin forever. The copy stays truthful: the
    // payment did go through, so it says so and asks the user to check back.
    walletGet.mockResolvedValue(wallet({ total_balance: 500 }));
    renderWallet({ awaitingGrant: true });
    await waitFor(
      () =>
        expect(
          document.querySelector('[data-slot="billing-wallet-slow"]'),
        ).not.toBeNull(),
      { timeout: 3000 },
    );
    expect(
      document.querySelector('[data-slot="billing-wallet-pending"]'),
    ).toBeNull();
  });

  it("does not poll at all on a normal visit", async () => {
    walletGet.mockResolvedValue(wallet());
    renderWallet();
    await waitFor(() =>
      expect(
        document.querySelector('[data-slot="billing-wallet"]'),
      ).not.toBeNull(),
    );
    const afterLoad = walletGet.mock.calls.length;
    await new Promise((r) => setTimeout(r, 60));
    expect(walletGet.mock.calls.length).toBe(afterLoad);
  });
});
