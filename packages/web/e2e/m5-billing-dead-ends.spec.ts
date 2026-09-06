import { expect, test } from "@playwright/test";

/**
 * Spec M5 (T7) — every credit warning leads somewhere, proven in a real browser.
 *
 * T6 closed three dead ends (D-M5-7). Two are server-rendered and one is not: the shell
 * watcher fires client-side on load, reads `GET /v1/me/credits`, and pushes a
 * notification carrying the billing href. Markup alone cannot prove that one, so it is
 * proven here by opening the bell and following the link.
 *
 * This runs against the real stack: a real Clerk-authenticated session (auth.setup.ts)
 * against an API that verifies the JWT for real (an unauthenticated request 401s), and a
 * real Postgres. The balance is seeded out-of-band per scenario, since no endpoint lets a
 * caller set their own balance, which is correct.
 *
 * Scenario is selected by env so the harness can drive each state without a rebuild:
 *   M5_SCENARIO=low  → low balance, positive: card CTA + watcher
 *   M5_SCENARIO=zero → exhausted: the 402 CTA
 */

const SCENARIO = process.env.M5_SCENARIO ?? "low";

test.describe("M5 dead ends", () => {
  test.skip(SCENARIO !== "low", "low-balance scenario only");

  test("the low-balance card offers a way to act on it", async ({ page }) => {
    await page.goto("/settings");

    const card = page.locator('[data-slot="settings-low-balance-warning"]');
    await expect(card).toBeVisible();

    const cta = page.locator('[data-slot="settings-low-balance-action"] a');
    await expect(cta).toBeVisible();
    await expect(cta).toHaveAttribute("href", "/settings/billing");

    // Following it has to actually arrive somewhere that sells credits.
    await cta.click();
    await expect(page).toHaveURL(/\/settings\/billing$/);
    await expect(page.locator('[data-slot="billing-packs"]')).toBeVisible();
  });

  test("the shell watcher's notification links to billing", async ({
    page,
  }) => {
    // The client-side dead end. The watcher fires once per session on load, so this
    // asserts the notification it actually produced in the browser, not a rendered
    // template. sessionStorage is per-context and fresh here.
    await page.goto("/personas");

    // The bell is the durable half of the notification (persist: true).
    await page.getByRole("button", { name: "Notifications" }).first().click();

    const entry = page
      .locator('[data-slot="notification-entry"]')
      .filter({ hasText: "Low balance" })
      .first();
    await expect(entry).toBeVisible();

    const link = entry.locator("a");
    await expect(link).toHaveAttribute("href", "/settings/billing");

    await link.click();
    await expect(page).toHaveURL(/\/settings\/billing$/);
  });
});

test.describe("M5 dead ends (exhausted)", () => {
  test.skip(SCENARIO !== "zero", "exhausted scenario only");

  test("the 402 cliff offers a way out", async ({ page }) => {
    await page.goto("/settings");

    const errorState = page.locator('[data-slot="error-state"]');
    await expect(errorState).toBeVisible();
    await expect(errorState).toHaveAttribute("data-status", "402");

    // At zero the low-balance card must NOT also render: the zero-guard picks one.
    await expect(
      page.locator('[data-slot="settings-low-balance-warning"]'),
    ).toHaveCount(0);

    const cta = page.locator('[data-slot="error-state-action"] a');
    await expect(cta).toBeVisible();
    await expect(cta).toHaveAttribute("href", "/settings/billing");

    await cta.click();
    await expect(page).toHaveURL(/\/settings\/billing$/);
  });
});
