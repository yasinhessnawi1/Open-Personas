import { expect, type Page, test } from "@playwright/test";

const API = process.env.E2E_API_BASE_URL ?? "http://127.0.0.1:8000";

const VALID_YAML = `schema_version: "1.0"
identity:
  name: Bjorn
  role: Concise assistant
  background: |
    Answers briefly and finishes quickly.
  language_default: en
  constraints:
    - Be concise.
self_facts:
  - fact: Prefers short answers.
    confidence: 1.0
worldview:
  - claim: Brevity is a virtue.
    domain: style
    epistemic: belief
    confidence: 0.9
    valid_time: always
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

test("run viewer streams an agentic run to a terminal status", async ({
  page,
}) => {
  await page.goto("/personas");
  const token = await clerkToken(page);
  const auth = {
    Authorization: `Bearer ${token}`,
    "Content-Type": "application/json",
  };

  // Seed a persona via the API.
  const personaRes = await page.request.post(`${API}/v1/personas`, {
    headers: auth,
    data: { yaml: VALID_YAML },
  });
  expect(personaRes.status()).toBe(201);
  const personaId = (await personaRes.json()).id as string;

  // Start a run from the persona detail page (the start-run entry → server action).
  await page.goto(`/personas/${personaId}`);
  await expect(page.getByText("Bjorn").first()).toBeVisible({
    timeout: 30_000,
  });
  await page
    .getByRole("textbox")
    .fill(
      "Reply with a one-sentence greeting, then finish. Do not ask questions.",
    );
  await page.getByRole("button", { name: "Start task" }).click();

  // Spec W1 (D-W1-3): the dispatch lands on the ad hoc task's detail; its run history links
  // into the viewer once the worker has opened the run.
  await page.waitForURL("**/activity/tasks/**", { timeout: 30_000 });
  await page
    .locator('[data-slot="run-history-row"] a')
    .first()
    .click({ timeout: 60_000 });
  await page.waitForURL("**/runs/**", { timeout: 30_000 });
  await expect(page.getByText("agentic run")).toBeVisible({ timeout: 30_000 });

  // The timeline reaches a terminal status (live DeepSeek; multi-step → generous).
  await expect(page.locator('[data-status="running"]')).toHaveCount(0, {
    timeout: 120_000,
  });
  // At least one step/final card rendered in the timeline.
  await expect(page.locator("ol > li").first()).toBeVisible();
});
