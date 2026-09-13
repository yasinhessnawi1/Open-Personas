import { expect, test } from "@playwright/test";

// The marquee authoring flow (spec 10, D-10-2): NL → live model DRAFT (no row
// yet) → clarifying-questions seam → refine round → edit → form↔YAML sync →
// save CREATES the persona → persisted + shown. (acceptance #3/#4 + part of #1.)
test("author a draft, refine it, edit, and save creates the persona", async ({
  page,
}) => {
  await page.goto("/personas/new");
  await expect(
    page.getByRole("heading", { name: "Describe your persona", exact: true }),
  ).toBeVisible({ timeout: 30_000 });

  await page
    .getByRole("textbox")
    .first()
    .fill(
      "A concise assistant who answers briefly and never gives legal advice.",
    );
  await page.getByRole("button", { name: "Generate persona" }).click();

  // The loading state reads as deliberate work (not a blank spinner).
  await expect(page.getByText("Authoring your persona")).toBeVisible({
    timeout: 15_000,
  });

  // The DRAFT returns (no persona row yet) and the structured form populates.
  await expect(
    page.getByRole("heading", { name: "Review your persona", exact: true }),
  ).toBeVisible({ timeout: 120_000 });
  await expect(page.getByLabel("Name")).not.toHaveValue("");

  // The spec-10 seam: clarifying questions render in the editor.
  await expect(
    page.getByText("Refine with clarifying questions"),
  ).toBeVisible();

  // Refine: answer the first question → the draft re-generates (live model). The
  // button shows "Refining…" during the call, then the editor re-mounts — so we
  // wait for "Refining…" to appear then disappear (the refine completed).
  await page
    .getByPlaceholder("Your answer…")
    .first()
    .fill("Yes — keep answers under three sentences.");
  await page
    .getByRole("button", { name: "Apply", exact: true })
    .first()
    .click();
  await expect(
    page.getByRole("button", { name: "Refining…" }).first(),
  ).toBeVisible({ timeout: 30_000 });
  await expect(page.getByRole("button", { name: "Refining…" })).toHaveCount(0, {
    timeout: 120_000,
  });

  // Edit the (re-mounted) name → toggle raw YAML → Monaco reflects the edit.
  const nameInput = page.getByLabel("Name");
  await nameInput.fill("E2E Edited Persona");
  await page.getByRole("button", { name: "Edit raw YAML" }).click();
  await expect(page.locator(".monaco-editor")).toBeVisible({ timeout: 30_000 });
  await expect(page.locator(".monaco-editor")).toContainText(
    "E2E Edited Persona",
    { timeout: 15_000 },
  );

  // Save → CREATES the persona (no row existed) → redirect to its detail page.
  await page.getByRole("button", { name: "Save persona" }).click();
  await expect(
    page.getByRole("heading", { name: "E2E Edited Persona", exact: true }),
  ).toBeVisible({ timeout: 30_000 });
});
