/**
 * Spec 31 T5 — <BudgetIndicator> tests (D-31-2).
 *
 * Three states tied to what Spec 23 enforces: per-session soft cap (meter +
 * approaching note at the real 0.8 knee), per-turn hard cap (informational),
 * per-day deferred (honest fail-loud note). Nothing when no snapshot.
 */
import { render } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it } from "vitest";
import messages from "@/i18n/messages/en.json";
import type { BudgetSnapshot } from "@/lib/sse-types";
import { BudgetIndicator } from "./budget-indicator";

function renderBudget(budget?: BudgetSnapshot) {
  return render(
    <NextIntlClientProvider locale="en" messages={messages}>
      <BudgetIndicator budget={budget} />
    </NextIntlClientProvider>,
  );
}

describe("BudgetIndicator", () => {
  it("renders nothing without a snapshot (rule-based / no-cap turns)", () => {
    const { container } = renderBudget(undefined);
    expect(
      container.querySelector('[data-slot="budget-indicator"]'),
    ).toBeNull();
  });

  it("shows session spend vs cap when below the approach knee (no note)", () => {
    const { container, getByText } = renderBudget({
      session_spent_cents: 10,
      max_cents_per_session: 50,
    });
    expect(getByText(/10\.00¢ of 50\.00¢/)).toBeInTheDocument();
    expect(
      container
        .querySelector('[data-slot="budget-session"]')
        ?.getAttribute("data-approaching"),
    ).toBe("false");
    expect(
      container.querySelector('[data-slot="budget-approaching"]'),
    ).toBeNull();
  });

  it("surfaces the approaching note at the real 0.8 session knee", () => {
    const { container } = renderBudget({
      session_spent_cents: 40, // 40/50 == 0.8
      max_cents_per_session: 50,
    });
    expect(
      container
        .querySelector('[data-slot="budget-session"]')
        ?.getAttribute("data-approaching"),
    ).toBe("true");
    expect(
      container.querySelector('[data-slot="budget-approaching"]'),
    ).not.toBeNull();
  });

  it("shows the per-turn cap as an informational line when set", () => {
    const { getByText } = renderBudget({
      session_spent_cents: 1,
      max_cents_per_turn: 3,
    });
    expect(getByText(/Per-turn cap 3\.00/)).toBeInTheDocument();
  });

  it("surfaces the per-day fail-loud note honestly when a per-day cap is set", () => {
    const { container } = renderBudget({
      session_spent_cents: 1,
      max_cents_per_day: 100,
    });
    expect(
      container.querySelector('[data-slot="budget-perday-unenforced"]'),
    ).not.toBeNull();
  });
});
