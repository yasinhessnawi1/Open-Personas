/**
 * Spec F? L6a — LowBalanceWarningCard tests.
 *
 * Verifies the three branches:
 *   - low_balance=false → no warning rendered (null);
 *   - low_balance=true, balance>0 → warning visible;
 *   - low_balance=true, balance=0 → no warning (the credits-exhausted cliff is
 *     a separate surface on the page).
 */

import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { LowBalanceWarningCard } from "./low-balance-warning-card";

const COPY = { title: "Credits running low", hint: "Top up soon." };

describe("LowBalanceWarningCard", () => {
  it("renders nothing when low_balance is false", () => {
    const { container } = render(
      <LowBalanceWarningCard
        credits={{ balance: 50_000, low_balance: false }}
        {...COPY}
      />,
    );
    expect(
      container.querySelector('[data-slot="settings-low-balance-warning"]'),
    ).toBeNull();
  });

  it("renders the warning when low_balance is true and balance > 0", () => {
    const { container } = render(
      <LowBalanceWarningCard
        credits={{ balance: 5_000, low_balance: true }}
        {...COPY}
      />,
    );
    const card = container.querySelector(
      '[data-slot="settings-low-balance-warning"]',
    );
    expect(card).not.toBeNull();
    expect(card?.textContent).toContain("Credits running low");
    expect(card?.textContent).toContain("Top up soon.");
  });

  it("renders nothing when balance is 0 (credits-exhausted cliff is handled elsewhere)", () => {
    const { container } = render(
      <LowBalanceWarningCard
        credits={{ balance: 0, low_balance: true }}
        {...COPY}
      />,
    );
    expect(
      container.querySelector('[data-slot="settings-low-balance-warning"]'),
    ).toBeNull();
  });

  // --- Spec M5 (T6): the warning became something you can act on -------------

  it("renders the action the caller passes", () => {
    // D-M5-7: this card used to state the problem and stop. The CTA is what turns
    // "you are running out" into something the user can resolve.
    const { container } = render(
      <LowBalanceWarningCard
        credits={{ balance: 500, low_balance: true }}
        {...COPY}
        action={<a href="/settings/billing">Add credits</a>}
      />,
    );
    const slot = container.querySelector(
      '[data-slot="settings-low-balance-action"]',
    );
    expect(slot).not.toBeNull();
    expect(slot?.querySelector("a")?.getAttribute("href")).toBe(
      "/settings/billing",
    );
  });

  it("renders exactly as before when no action is passed", () => {
    // The slot is optional so the card stays usable where a CTA would be wrong,
    // and so every existing caller keeps working untouched.
    const { container } = render(
      <LowBalanceWarningCard
        credits={{ balance: 500, low_balance: true }}
        {...COPY}
      />,
    );
    expect(
      container.querySelector('[data-slot="settings-low-balance-warning"]'),
    ).not.toBeNull();
    expect(
      container.querySelector('[data-slot="settings-low-balance-action"]'),
    ).toBeNull();
  });

  it("shows no action when the card itself is hidden", () => {
    // A CTA must not leak out of a card that is not rendering.
    const { container } = render(
      <LowBalanceWarningCard
        credits={{ balance: 0, low_balance: true }}
        {...COPY}
        action={<a href="/settings/billing">Add credits</a>}
      />,
    );
    expect(container.firstChild).toBeNull();
  });
});
