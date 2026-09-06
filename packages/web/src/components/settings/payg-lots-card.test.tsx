/**
 * Spec M5 (T5) — PAYG lots and their expiry (D-M5-13).
 *
 * The failure this closes: a user buys $50 of credits and watches them disappear a year
 * later with no warning. M4 built the lots, the expiry dates and the FIFO order, and the
 * wallet has served them since T2a, but nothing rendered them (§1c.9).
 *
 * Two properties carry the weight:
 *
 * 1. **The order is the server's**, never re-sorted here. The lots come back in the order
 *    they will actually be spent, so re-sorting would make the page claim a spend order
 *    the ledger does not follow.
 * 2. **Approaching expiry is visible while the user can still act on it.** A date alone
 *    is not a warning; nobody diffs dates against today.
 */

import { render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it } from "vitest";
import messages from "@/i18n/messages/en.json";
import { daysUntil, PaygLotsCard } from "./payg-lots-card";

const NOW = Date.parse("2026-09-04T00:00:00Z");

function lot(daysFromNow: number, remaining = 500, total = 500) {
  return {
    credits_remaining: remaining,
    credits_total: total,
    expires_at: new Date(NOW + daysFromNow * 86_400_000).toISOString(),
  };
}

function renderLots(lots: ReturnType<typeof lot>[]) {
  return render(
    <NextIntlClientProvider locale="en" messages={messages}>
      <PaygLotsCard lots={lots} now={NOW} />
    </NextIntlClientProvider>,
  );
}

function lotEls(): Element[] {
  return Array.from(document.querySelectorAll('[data-slot="payg-lot"]'));
}

describe("payg lots", () => {
  it("shows every live lot", () => {
    renderLots([lot(300), lot(200), lot(100)]);
    expect(lotEls().length).toBe(3);
  });

  it("shows what remains and what the pack started as", () => {
    // Partial spend has to be visible: "200 left" alone hides that it was a $5 pack.
    renderLots([lot(300, 200, 500)]);
    expect(screen.getByText(/200 of 500 credits left/)).toBeTruthy();
  });

  it("shows a real expiry date, not just a countdown", () => {
    renderLots([lot(300)]);
    expect(screen.getByText(/Expires .*2027/)).toBeTruthy();
  });

  it("preserves the server's FIFO order rather than re-sorting", () => {
    // The server returns oldest-expiring first. If this component sorted, it would claim
    // a spend order the ledger does not follow. Fed deliberately unsorted to prove the
    // rendering is a pass-through.
    const unsorted = [lot(300, 100), lot(10, 200), lot(150, 300)];
    renderLots(unsorted);
    const remainings = lotEls().map(
      (el) => el.querySelector("p")?.textContent?.match(/^(\d+)/)?.[1],
    );
    expect(remainings).toEqual(["100", "200", "300"]);
  });

  it("warns on a lot that is about to lapse", () => {
    renderLots([lot(10)]);
    const el = lotEls()[0];
    expect(el.getAttribute("data-expiring-soon")).toBe("true");
    expect(el.querySelector('[data-slot="payg-lot-warning"]')).not.toBeNull();
  });

  it("does not warn on a lot with most of its year left", () => {
    // Warning on everything is the same as warning on nothing.
    renderLots([lot(300)]);
    const el = lotEls()[0];
    expect(el.getAttribute("data-expiring-soon")).toBe("false");
    expect(el.querySelector('[data-slot="payg-lot-warning"]')).toBeNull();
  });

  it("warns only on the lots that need it", () => {
    renderLots([lot(5), lot(200)]);
    const flags = lotEls().map((el) => el.getAttribute("data-expiring-soon"));
    expect(flags).toEqual(["true", "false"]);
  });

  it("says a lot expiring today expires today, not in 0 days", () => {
    renderLots([lot(0)]);
    expect(screen.getByText(/Expires today/)).toBeTruthy();
  });

  it("never shows a negative countdown", () => {
    // A lot at the boundary can round negative between the server read and the render.
    // "-2 days left" reads as a bug; the server is the authority on liveness.
    renderLots([lot(-2)]);
    const warning = document.querySelector('[data-slot="payg-lot-warning"]');
    expect(warning?.textContent).not.toMatch(/-/);
  });

  it("renders nothing when no packs were bought", () => {
    // An empty box is worse than absence: the packs card above already invites the
    // first purchase.
    const { container } = renderLots([]);
    expect(container.firstChild).toBeNull();
  });

  it("counts whole days remaining", () => {
    expect(daysUntil(new Date(NOW + 3 * 86_400_000).toISOString(), NOW)).toBe(
      3,
    );
    expect(daysUntil(new Date(NOW).toISOString(), NOW)).toBe(0);
  });
});
