import { expect, test } from "@playwright/test";

// Runs authed (reuses the saved storageState from auth.setup).
test.describe("app shell", () => {
  test("desktop: sidebar nav navigates + theme toggle switches to dark", async ({
    page,
  }) => {
    await page.goto("/personas");

    // Nav rows may carry live count badges (R9-010), so match on the leading
    // label rather than an exact accessible name.
    const personasLink = page.getByRole("link", { name: /^Personas/ }).first();
    // R9-009: the Conversations nav row folded into the MESSAGES section —
    // its "All chats (N)" header link is the way to /conversations now.
    const allChatsLink = page.getByRole("link", { name: /^All chats/ });
    const settingsLink = page.getByRole("link", {
      name: "Settings",
      exact: true,
    });
    await expect(personasLink).toBeVisible();
    await expect(allChatsLink).toBeVisible();
    await expect(settingsLink).toBeVisible();

    await allChatsLink.click();
    await expect(page).toHaveURL(/\/conversations$/);

    // Theme toggle → Dark applies the `dark` class on <html>.
    await page.getByRole("button", { name: "Toggle theme" }).click();
    await page.getByRole("menuitem", { name: "Dark" }).click();
    await expect(page.locator("html")).toHaveClass(/dark/);
  });

  test("mobile (375px): sheet nav opens and navigates", async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 800 });
    await page.goto("/personas");

    // Desktop sidebar is hidden; open the mobile sheet.
    await page.getByRole("button", { name: "Open menu" }).click();
    const settingsLink = page.getByRole("link", {
      name: "Settings",
      exact: true,
    });
    await expect(settingsLink).toBeVisible();
    await settingsLink.click();
    await expect(page).toHaveURL(/\/settings$/);
  });
});
