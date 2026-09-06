/**
 * Spec M5 (T2c) — renewal truth + the past-due banner (D-M5-10 / D-M5-14).
 *
 * The failure this closes: a Pro user whose card expires is silently moved to `past_due`,
 * their allowance quietly stops renewing, and the app never says why they are running
 * dry. So the banner has to appear on exactly that state and say what happened.
 */

import { render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it, vi } from "vitest";
import messages from "@/i18n/messages/en.json";
import { SubscriptionStatusCard } from "./subscription-status-card";

type CardWallet = Parameters<typeof SubscriptionStatusCard>[0]["wallet"];

function state(overrides: Partial<CardWallet> = {}): CardWallet {
  return {
    plan_code: "pro",
    current_period_end: "2026-09-01T00:00:00Z",
    cancel_at_period_end: false,
    subscription_status: "active",
    ...overrides,
  };
}

function renderCard(wallet: CardWallet, onManage?: () => void) {
  return render(
    <NextIntlClientProvider locale="en" messages={messages}>
      <SubscriptionStatusCard wallet={wallet} onManage={onManage} />
    </NextIntlClientProvider>,
  );
}

describe("subscription status card", () => {
  it("tells a subscriber when the plan renews", () => {
    renderCard(state());
    expect(document.querySelector('[data-slot="renewal-date"]')).not.toBeNull();
    expect(screen.getByText(messages.billing.renewsLabel)).toBeTruthy();
  });

  it("says a cancelling plan ENDS, and still shows the date", () => {
    // Both facts together: "cancelling" alone reads as if access stops now; a date alone
    // hides that it is the last one.
    renderCard(state({ cancel_at_period_end: true }));
    expect(screen.getByText(messages.billing.endsLabel)).toBeTruthy();
    expect(document.querySelector('[data-slot="renewal-date"]')).not.toBeNull();
    expect(
      document.querySelector('[data-slot="cancelling-note"]'),
    ).not.toBeNull();
  });

  it("shows the past-due banner when a payment failed", () => {
    renderCard(state({ subscription_status: "past_due" }));
    expect(
      document.querySelector('[data-slot="past-due-title"]'),
    ).not.toBeNull();
    expect(screen.getByText(messages.billing.pastDueBody)).toBeTruthy();
  });

  it("past-due copy explains the consequence, not just the failure", () => {
    // The user's real question is "why am I running dry", so the copy has to connect the
    // failed payment to the allowance not renewing.
    expect(messages.billing.pastDueBody.toLowerCase()).toContain("renew");
  });

  it("offers the portal CTA on past due", () => {
    const onManage = vi.fn();
    renderCard(state({ subscription_status: "past_due" }), onManage);
    const cta = document.querySelector('[data-slot="past-due-cta"]');
    expect(cta).not.toBeNull();
    (cta as HTMLButtonElement).click();
    expect(onManage).toHaveBeenCalledOnce();
  });

  it("renders nothing for a free caller", () => {
    // No subscription: nothing renews and nothing can be past due, so an empty card would
    // make a claim about a plan they do not have.
    const { container } = renderCard(
      state({ plan_code: "free", current_period_end: null }),
    );
    expect(container.firstChild).toBeNull();
  });

  it("still warns a past-due caller with no renewal date", () => {
    // past_due must win over the free-caller shortcut: the banner is the whole point.
    renderCard(
      state({ current_period_end: null, subscription_status: "past_due" }),
    );
    expect(
      document.querySelector('[data-slot="past-due-title"]'),
    ).not.toBeNull();
  });
});
