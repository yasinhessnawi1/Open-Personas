/**
 * Spec M5 (T3) — plan picker, packs, portal (D-M5-4/5/6).
 *
 * Two things these guard that are easy to get wrong:
 *
 * 1. **Nothing money-shaped is posted.** The client sends a plan/pack CODE and nothing
 *    else; the server derives price, credit amount and Stripe Price id. A client-supplied
 *    price is the one bug in this task that costs real money, so the request bodies are
 *    asserted exactly, not loosely.
 * 2. **No dead affordances.** Free renders as a tier with no button (the backend 400s it),
 *    a failed checkout surfaces honest copy instead of a stuck spinner, and a disabled
 *    billing surface renders nothing at all.
 */

import { render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import messages from "@/i18n/messages/en.json";
import { BillingPlans } from "./billing-plans";

const apiGet = vi.fn();
const apiPost = vi.fn();

vi.mock("@/lib/api/use-api", () => ({
  useApi: () => ({ GET: apiGet, POST: apiPost }),
}));

const CATALOG = {
  enabled: true,
  publishable_key: "pk_test",
  plans: [
    {
      code: "free",
      monthly_price_credits: 0,
      included_allowance_credits: 300,
      auto_topup_eligible: false,
      is_default: true,
    },
    {
      code: "plus",
      monthly_price_credits: 1500,
      included_allowance_credits: 2000,
      auto_topup_eligible: false,
      is_default: false,
    },
    {
      code: "pro",
      monthly_price_credits: 5000,
      included_allowance_credits: 6000,
      auto_topup_eligible: true,
      is_default: false,
    },
  ],
  packs: [
    { code: "5", price_credits: 500, granted_credits: 500, expiry_months: 12 },
    {
      code: "10",
      price_credits: 1000,
      granted_credits: 1000,
      expiry_months: 12,
    },
  ],
  auto_topup_threshold_credits: 200,
  auto_topup_amount_credits: 1000,
};

function ok(data: unknown) {
  return { response: new Response(null, { status: 200 }), data };
}

function setup(planCode = "free", catalog: typeof CATALOG = CATALOG) {
  apiGet.mockImplementation((path: string) =>
    Promise.resolve(
      path === "/v1/billing/config"
        ? ok(catalog)
        : ok({
            total_balance: 500,
            allowance_balance: 500,
            plan_code: planCode,
          }),
    ),
  );
}

function renderPlans() {
  return render(
    <NextIntlClientProvider locale="en" messages={messages}>
      <BillingPlans />
    </NextIntlClientProvider>,
  );
}

/** Each plan row's terms line, keyed by plan code. */
function planTermsText(): Record<string, string> {
  return Object.fromEntries(
    Array.from(document.querySelectorAll('[data-slot="plan-row"]')).map(
      (row) => [
        row.getAttribute("data-plan") ?? "",
        row.querySelector('[data-slot="plan-terms"]')?.textContent ?? "",
      ],
    ),
  );
}

function packLabels(): string[] {
  return Array.from(document.querySelectorAll('[data-slot="pack-cta"]')).map(
    (el) => el.textContent ?? "",
  );
}

const assign = vi.fn();

beforeEach(() => {
  Object.defineProperty(window, "location", {
    value: { assign },
    writable: true,
  });
});

afterEach(() => {
  apiGet.mockReset();
  apiPost.mockReset();
  assign.mockReset();
});

describe("billing plans", () => {
  it("renders every plan from the catalog", async () => {
    setup();
    renderPlans();
    await waitFor(() =>
      expect(document.querySelectorAll('[data-slot="plan-row"]').length).toBe(
        3,
      ),
    );
  });

  it("says what each plan gives in dollars, with the extra over its price", async () => {
    // R9-177 B6 (owner ruling 2026-09-26): one unit, and the extra is computed from the
    // catalog. Exact strings, because a value claim must be exactly right.
    setup();
    renderPlans();
    await waitFor(() =>
      expect(document.querySelectorAll('[data-slot="plan-terms"]').length).toBe(
        3,
      ),
    );
    expect(planTermsText()).toEqual({
      free: "$3 to spend every month, on us.",
      plus: "$20 to spend every month for $15. That's $5 extra.",
      pro: "$60 to spend every month for $50. That's $10 extra.",
    });
  });

  it("drops the extra claim when a plan gives no more than it costs", async () => {
    setup("free", {
      ...CATALOG,
      plans: [
        {
          ...CATALOG.plans[1],
          monthly_price_credits: 1550,
          included_allowance_credits: 1550,
        },
      ],
    });
    renderPlans();
    await waitFor(() =>
      expect(planTermsText().plus).toBe(
        "$15.50 to spend every month for $15.50.",
      ),
    );
  });

  it("labels each pack with the one amount it adds", async () => {
    setup();
    renderPlans();
    await waitFor(() =>
      expect(document.querySelectorAll('[data-slot="pack-cta"]').length).toBe(
        2,
      ),
    );
    expect(packLabels()).toEqual(["Add $5", "Add $10"]);
  });

  it("shows both figures for a pack that grants a different amount", async () => {
    setup("free", {
      ...CATALOG,
      packs: [
        {
          code: "10",
          price_credits: 1000,
          granted_credits: 1250,
          expiry_months: 12,
        },
      ],
    });
    renderPlans();
    await waitFor(() =>
      expect(packLabels()).toEqual(["$10 for $12.50 to spend"]),
    );
  });

  it.each(["free", "plus"])(
    "states that the monthly amount does not carry over (on %s)",
    async (planCode) => {
      // Owner ruling 2026-09-26: one note for every plan, Free included, which has no
      // renewal date to name. The months come from the catalog.
      setup(planCode);
      renderPlans();
      await waitFor(() =>
        expect(
          document.querySelector('[data-slot="plan-renewal-note"]')
            ?.textContent,
        ).toBe(
          "Your monthly amount refreshes each month. What you don't use doesn't carry over; pack credit lasts 12 months.",
        ),
      );
    },
  );

  it("still states the monthly terms when the catalog has no packs", async () => {
    // The note used to vanish with the packs; the plans' own terms do not depend on them.
    setup("free", { ...CATALOG, packs: [] });
    renderPlans();
    await waitFor(() =>
      expect(
        document.querySelector('[data-slot="plan-renewal-note"]')?.textContent,
      ).toBe(
        "Your monthly amount refreshes each month. What you don't use doesn't carry over.",
      ),
    );
  });

  it("gives Free no purchase button", async () => {
    // D-M5-4: the backend 400s a free checkout, so a button would be a dead end.
    setup();
    renderPlans();
    await waitFor(() =>
      expect(document.querySelectorAll('[data-slot="plan-row"]').length).toBe(
        3,
      ),
    );
    expect(
      document.querySelector('[data-slot="plan-cta"][data-plan="free"]'),
    ).toBeNull();
  });

  it("gives the current plan no purchase button", async () => {
    setup("pro");
    renderPlans();
    await waitFor(() =>
      expect(
        document.querySelector('[data-plan="pro"][data-current="true"]'),
      ).not.toBeNull(),
    );
    expect(
      document.querySelector('[data-slot="plan-cta"][data-plan="pro"]'),
    ).toBeNull();
  });

  it("posts ONLY the plan code, never a price", async () => {
    setup();
    apiPost.mockResolvedValue(ok({ url: "https://checkout.stripe.test/s" }));
    renderPlans();
    await waitFor(() =>
      expect(
        document.querySelector('[data-slot="plan-cta"][data-plan="plus"]'),
      ).not.toBeNull(),
    );
    (
      document.querySelector(
        '[data-slot="plan-cta"][data-plan="plus"]',
      ) as HTMLButtonElement
    ).click();

    await waitFor(() => expect(apiPost).toHaveBeenCalled());
    const [path, init] = apiPost.mock.calls[0];
    expect(path).toBe("/v1/billing/checkout");
    // Exactly one key. Any economics in this body would be a money bug.
    expect(init.body).toEqual({ plan_code: "plus" });
  });

  it("posts ONLY the pack code, never a credit amount", async () => {
    setup();
    apiPost.mockResolvedValue(ok({ url: "https://checkout.stripe.test/p" }));
    renderPlans();
    await waitFor(() =>
      expect(
        document.querySelector('[data-slot="pack-cta"][data-pack="10"]'),
      ).not.toBeNull(),
    );
    (
      document.querySelector(
        '[data-slot="pack-cta"][data-pack="10"]',
      ) as HTMLButtonElement
    ).click();

    await waitFor(() => expect(apiPost).toHaveBeenCalled());
    const [path, init] = apiPost.mock.calls[0];
    expect(path).toBe("/v1/billing/checkout/pack");
    expect(init.body).toEqual({ pack: "10" });
  });

  it("redirects to the Stripe url the server returned", async () => {
    setup();
    apiPost.mockResolvedValue(
      ok({ url: "https://checkout.stripe.test/session_x" }),
    );
    renderPlans();
    await waitFor(() =>
      expect(
        document.querySelector('[data-slot="pack-cta"][data-pack="5"]'),
      ).not.toBeNull(),
    );
    (
      document.querySelector(
        '[data-slot="pack-cta"][data-pack="5"]',
      ) as HTMLButtonElement
    ).click();
    await waitFor(() =>
      expect(assign).toHaveBeenCalledWith(
        "https://checkout.stripe.test/session_x",
      ),
    );
  });

  it("states the pack expiry term at purchase", async () => {
    // D-M5-13: bought credits really do expire, so the term ships with the offer rather
    // than being discovered after they vanish.
    setup();
    renderPlans();
    await waitFor(() =>
      expect(
        document.querySelector('[data-slot="pack-expiry-note"]'),
      ).not.toBeNull(),
    );
    expect(
      document.querySelector('[data-slot="pack-expiry-note"]')?.textContent,
    ).toMatch(/12 months/);
  });

  it("surfaces an honest error when checkout cannot open", async () => {
    // The backend 400s an unconfigured Price. Never a dead spinner.
    setup();
    apiPost.mockRejectedValue(new Error("400"));
    renderPlans();
    await waitFor(() =>
      expect(
        document.querySelector('[data-slot="pack-cta"][data-pack="5"]'),
      ).not.toBeNull(),
    );
    (
      document.querySelector(
        '[data-slot="pack-cta"][data-pack="5"]',
      ) as HTMLButtonElement
    ).click();

    await waitFor(() =>
      expect(
        screen.getByText(messages.billing.checkoutUnavailable),
      ).toBeTruthy(),
    );
    expect(assign).not.toHaveBeenCalled();
    // The spinner cleared: the button is usable again.
    const cta = document.querySelector(
      '[data-slot="pack-cta"][data-pack="5"]',
    ) as HTMLButtonElement;
    expect(cta.disabled).toBe(false);
  });

  it("shows manage-subscription only to a subscriber", async () => {
    setup("plus");
    renderPlans();
    await waitFor(() =>
      expect(
        document.querySelector('[data-slot="manage-subscription"]'),
      ).not.toBeNull(),
    );
  });

  it("hides manage-subscription from a free user", async () => {
    setup("free");
    renderPlans();
    await waitFor(() =>
      expect(document.querySelectorAll('[data-slot="plan-row"]').length).toBe(
        3,
      ),
    );
    expect(
      document.querySelector('[data-slot="manage-subscription"]'),
    ).toBeNull();
  });

  it("renders nothing at all when billing is disabled", async () => {
    // D-M5-3: community must have no purchase affordance to click.
    apiGet.mockRejectedValue(new Error("404"));
    const { container } = renderPlans();
    await waitFor(() => expect(container.firstChild).toBeNull());
    expect(container.querySelectorAll("button").length).toBe(0);
  });
});
