/**
 * Spec M5 (T4a) — the auto-top-up toggle (B1).
 *
 * Auto-top-up is the first thing in this product that spends money with no human
 * present, so the guarantees here are about what the browser is allowed to say and send:
 *
 * 1. **Nothing money-shaped is posted.** The body carries the INTENT only. A client that
 *    could name its own top-up amount would be naming its own charge.
 * 2. **The switch shows the STORED state**, not the requested one, so a refused change
 *    snaps back instead of leaving the UI claiming something the engine will not honour.
 * 3. **The numbers come from the API.** The copy states the real threshold and amount
 *    (D-M4-R5) rather than a hardcoded "$2/$10" that would drift the moment the owner
 *    changes them.
 */

import { render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it, vi } from "vitest";
import messages from "@/i18n/messages/en.json";
import { AutoTopupCard } from "./auto-topup-card";

const apiPatch = vi.fn();

vi.mock("@/lib/api/use-api", () => ({
  useApi: () => ({ PATCH: apiPatch }),
}));

/** An openapi-fetch success result, the shape `unwrap` actually consumes. */
function ok(data: unknown) {
  return { response: new Response(null, { status: 200 }), data };
}

function renderCard(
  overrides: Partial<Parameters<typeof AutoTopupCard>[0]> = {},
) {
  return render(
    <NextIntlClientProvider locale="en" messages={messages}>
      <AutoTopupCard
        planCode="pro"
        eligible
        initialEnabled={false}
        thresholdCredits={200}
        amountCredits={1000}
        {...overrides}
      />
    </NextIntlClientProvider>,
  );
}

function theSwitch(): HTMLButtonElement {
  return document.querySelector(
    '[data-slot="auto-topup-switch"]',
  ) as HTMLButtonElement;
}

afterEach(() => {
  apiPatch.mockReset();
});

describe("auto top-up toggle", () => {
  it("posts ONLY the intent, never an amount", async () => {
    apiPatch.mockResolvedValue(
      ok({ enabled: true, threshold_credits: 200, amount_credits: 1000 }),
    );
    renderCard();
    theSwitch().click();

    await waitFor(() => expect(apiPatch).toHaveBeenCalled());
    const [path, init] = apiPatch.mock.calls[0];
    expect(path).toBe("/v1/me/billing/auto-topup");
    // Exactly one key. Any economics in this body would be a money bug.
    expect(init.body).toEqual({ enabled: true });
  });

  it("arms the switch when the server confirms", async () => {
    apiPatch.mockResolvedValue(
      ok({ enabled: true, threshold_credits: 200, amount_credits: 1000 }),
    );
    renderCard();
    theSwitch().click();
    await waitFor(() =>
      expect(theSwitch().getAttribute("aria-checked")).toBe("true"),
    );
  });

  it("shows the STORED state, not the requested one", async () => {
    // The server is the authority: if it refuses or clamps, the switch must show what is
    // actually armed rather than what the click asked for.
    apiPatch.mockResolvedValue(
      ok({ enabled: false, threshold_credits: 200, amount_credits: 1000 }),
    );
    renderCard();
    theSwitch().click();
    await waitFor(() => expect(apiPatch).toHaveBeenCalled());
    expect(theSwitch().getAttribute("aria-checked")).toBe("false");
  });

  it("snaps back and says nothing was charged when the change fails", async () => {
    apiPatch.mockRejectedValue(new Error("409"));
    renderCard({ initialEnabled: true });
    theSwitch().click();

    await waitFor(() =>
      expect(screen.getByText(messages.billing.autoTopupFailed)).toBeTruthy(),
    );
    // Reverted to the last state the server confirmed, not left mid-flight.
    expect(theSwitch().getAttribute("aria-checked")).toBe("true");
  });

  it("states the real threshold and amount from the API", async () => {
    // D-M5-8: the UI says what it actually armed. Hardcoded "$2/$10" copy would drift
    // silently the moment the owner changes the constants.
    renderCard({ thresholdCredits: 500, amountCredits: 2500 });
    expect(screen.getByText(/\$5/)).toBeTruthy();
    expect(screen.getByText(/\$25/)).toBeTruthy();
  });

  it("offers no switch on a plan that does not have it", async () => {
    // D-M5-28: eligibility is the catalog's answer. No dead control that 409s.
    renderCard({ eligible: false, planCode: "free" });
    expect(theSwitch()).toBeNull();
    expect(
      document.querySelector('[data-slot="auto-topup-unavailable"]'),
    ).not.toBeNull();
    expect(screen.getByText(messages.billing.autoTopupProOnly)).toBeTruthy();
  });

  it("does not call the API when the plan is ineligible", async () => {
    renderCard({ eligible: false, planCode: "free" });
    expect(apiPatch).not.toHaveBeenCalled();
  });

  it("reflects an already-armed toggle at load", async () => {
    renderCard({ initialEnabled: true });
    expect(theSwitch().getAttribute("aria-checked")).toBe("true");
  });

  it("disables the switch while a change is in flight", async () => {
    // Prevents a double-click firing two charges' worth of intent at the engine.
    let resolve: (v: unknown) => void = () => {};
    apiPatch.mockReturnValue(
      new Promise((r) => {
        resolve = r;
      }),
    );
    renderCard();
    theSwitch().click();

    await waitFor(() => expect(theSwitch().disabled).toBe(true));
    resolve(
      ok({ enabled: true, threshold_credits: 200, amount_credits: 1000 }),
    );
    await waitFor(() => expect(theSwitch().disabled).toBe(false));
  });
});
