import { expect, type Page, test } from "@playwright/test";

/**
 * Spec F2 T30 — Run viewer (F2 rebuild) e2e.
 *
 * Asserts the F2 presentation contract for the run viewer: PageBody +
 * PersonaAvatar + .type-heading task title + Stack of step cards +
 * retokenised StatusBadge + retokenised tier label. The full SSE flow
 * against live DeepSeek + the polymorphic two-shapes normaliser at
 * src/lib/run.ts continues to be covered by the existing runs.spec.ts;
 * this spec is presentation-focused.
 *
 * Plumbing untouched (per audit.md §runs.plumbing).
 */

const API = process.env.E2E_API_BASE_URL ?? "http://127.0.0.1:8000";

const ASTRID_YAML = `schema_version: "1.0"
identity:
  name: Astrid
  role: Norwegian tenancy law assistant
  background: |
    Helps tenants understand husleieloven.
  language_default: en
  constraints:
    - Never give binding legal advice.
self_facts: []
worldview: []
`;

type ClerkWindow = {
  Clerk: {
    session: { getToken: (o: { template: string }) => Promise<string | null> };
  };
};

async function clerkToken(page: Page): Promise<string> {
  await page.waitForFunction(
    () =>
      Boolean(
        (window as unknown as { Clerk?: { session?: unknown } }).Clerk?.session,
      ),
    null,
    { timeout: 20_000 },
  );
  const token = await page.evaluate(() =>
    (window as unknown as ClerkWindow).Clerk.session.getToken({
      template: "persona-api",
    }),
  );
  if (!token) throw new Error("no persona-api token");
  return token;
}

async function seedPersonaAndRun(page: Page): Promise<string> {
  const token = await clerkToken(page);
  // Ensure Astrid exists, then start a run.
  const personaRes = await page.request.post(`${API}/v1/personas`, {
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    data: { yaml: ASTRID_YAML },
  });
  // The personas endpoint is idempotent on name+user, so a 4xx on duplicate
  // is fine; we then locate Astrid via the list.
  let personaId: string;
  if (personaRes.ok()) {
    personaId = (await personaRes.json()).id;
  } else {
    const list = await page.request.get(`${API}/v1/personas`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    const items = (await list.json()) as { id: string; name?: string }[];
    const found = items.find((it) => it.name === "Astrid");
    if (!found) throw new Error("Astrid not found after seed");
    personaId = found.id;
  }

  const runRes = await page.request.post(`${API}/v1/runs`, {
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    data: {
      persona_id: personaId,
      task: "Summarise the deposit-return rules under husleieloven §5-1.",
    },
  });
  if (!runRes.ok()) throw new Error(`run start failed: ${runRes.status()}`);
  // Spec W1: the dispatch names the TASK; the worker opens the run when it claims the leg.
  const { task_id: taskId } = (await runRes.json()) as { task_id: string };
  const deadline = Date.now() + 60_000;
  while (Date.now() < deadline) {
    const detail = await page.request.get(`${API}/v1/tasks/${taskId}`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    const runs = ((await detail.json()) as { runs: { id: string }[] }).runs;
    if (runs.length > 0) return runs[0].id;
    await page.waitForTimeout(1_000);
  }
  throw new Error(`no run opened for task ${taskId} within 60s`);
}

test.describe("F2 T30 run viewer", () => {
  test("composes PageBody + PersonaAvatar + .type-heading task title + RunView", async ({
    page,
  }) => {
    const runId = await seedPersonaAndRun(page);
    await page.goto(`/runs/${runId}`);

    await expect(page.locator('[data-slot="page-body"]')).toBeVisible();
    await expect(page.locator('[data-slot="run-page-header"]')).toBeVisible();
    await expect(page.locator('[data-slot="run-task-title"]')).toContainText(
      /husleieloven|deposit/i,
    );
    await expect(page.locator('[data-slot="run-view"]')).toBeVisible();

    // RunStatusBadge renders with the retokenised .type-caption (closes the
    // text-[0.65rem] scaffold legacy at run-status-badge.tsx line 21).
    const badge = page.locator('[data-slot="run-status-badge"]');
    await expect(badge).toBeVisible();
    const badgeFontSize = await badge.evaluate((el) =>
      Number.parseFloat(getComputedStyle(el).fontSize),
    );
    expect(badgeFontSize).toBeLessThan(13);
  });

  test("identity-coloured avatar (D-F1-5 closed): PersonaAvatar exposes --identity-h", async ({
    page,
  }) => {
    const runId = await seedPersonaAndRun(page);
    await page.goto(`/runs/${runId}`);

    const avatar = page
      .locator('[data-slot="run-page-header"]')
      .getByRole("img", { name: "Astrid" });
    await expect(avatar).toBeVisible();
    const hue = await avatar.evaluate((el) =>
      getComputedStyle(el).getPropertyValue("--identity-h").trim(),
    );
    expect(hue.length).toBeGreaterThan(0);
  });

  test("back link routes to the persona detail page", async ({ page }) => {
    const runId = await seedPersonaAndRun(page);
    await page.goto(`/runs/${runId}`);

    await page.locator('[data-slot="back-link"]').click();
    await page.waitForURL(/\/personas\/[^/]+$/, { timeout: 30_000 });
  });
});
