import { expect, test } from "@playwright/test";

// The product app's `/` is the auth-aware product root (NOT a marketing page —
// the canonical marketing front door is the standalone marketing site, and
// there is no `/home` route). For a signed-in user `/` renders in place:
//   - HAS personas → the fast-launch dashboard ("Jump back in").
//   - NO personas  → the onboarding empty state.
// A signed-out visitor is redirected to the marketing site (or `/sign-in` as
// the fallback when NEXT_PUBLIC_MARKETING_URL is unset).
test("root renders the fast-launch dashboard for a signed-in user", async ({
  page,
}) => {
  // The e2e session is signed in (see auth.setup.ts).
  await page.goto("/");

  // `/` stays the URL — no `/home` redirect; the dashboard renders in place.
  await expect(page).toHaveURL(/\/$/, { timeout: 30_000 });

  // The fast-launch dashboard surfaces a "Jump back in" section with one-click
  // entry points (distinct from the `/personas` management grid).
  await expect(
    page.getByRole("heading", { name: "Jump back in", exact: true }),
  ).toBeVisible();
});
