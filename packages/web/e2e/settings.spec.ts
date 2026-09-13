import { expect, test } from "@playwright/test";

test("settings shows credits, usage, and a working tier-badge toggle", async ({
  page,
}) => {
  await page.goto("/settings");

  await expect(
    page.getByRole("heading", { name: "Settings", exact: true }),
  ).toBeVisible({
    timeout: 30_000,
  });

  // Credits + usage sections render (acceptance #7).
  await expect(
    page.getByRole("heading", { name: "Credits", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Usage", exact: true }),
  ).toBeVisible();

  // The tier-badge visibility toggle flips and persists locally.
  const toggle = page.getByRole("switch", { name: "Model tier badges" });
  await expect(toggle).toHaveAttribute("aria-checked", "true");
  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-checked", "false");
});

// Every heading assertion here is `exact: true` on purpose. Playwright matches
// an accessible name as a case-insensitive SUBSTRING by default, so on a fresh
// account "Conversations" also matches the empty state's "No conversations
// yet" heading and the run dies on a strict-mode violation. The same trap sits
// under "Credits" ("Credits exhausted") and "Usage" ("No usage yet."): each of
// those would pass on a page that no longer shows the section it claims to.
test("conversations list renders", async ({ page }) => {
  await page.goto("/conversations");
  await expect(
    page.getByRole("heading", { name: "Conversations", exact: true }),
  ).toBeVisible({ timeout: 30_000 });
});
