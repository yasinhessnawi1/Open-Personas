/**
 * Spec 28 — Rich Tool Output Delivery: live Playwright operator-pass.
 *
 * Run with the full stack (web :3000, API :8000, persona-pg :5436, E2B +
 * image backend, Clerk test-token auth). Each journey seeds its own persona,
 * drives a real conversation, and asserts the Spec 28 UI surfaces:
 *   - inline FileCard (data-slot="file-card")
 *   - right-panel renderer (data-slot="file-renderer-panel") on click
 *   - rendered↔raw toggle, Esc close
 *   - inline image thumbnail / inline diagram SVG
 * Screenshots are saved to docs/specs/phase2/spec_28/evidence/.
 *
 * Backend-gated journeys (image / E2B) skip + record 🟦 PARTIAL on 503.
 */

import { mkdirSync } from "node:fs";
import { join } from "node:path";
import { expect, type Page, test } from "@playwright/test";

const API = process.env.E2E_API_BASE_URL ?? "http://127.0.0.1:8000";
const EVIDENCE_DIR = "../../docs/specs/phase2/spec_28/evidence";

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

interface SeedOptions {
  name: string;
  role: string;
  tools?: string[];
  skills?: string[];
}

function personaYaml(opts: SeedOptions): string {
  const tools = opts.tools ?? [];
  const skills = opts.skills ?? [];
  const toolsBlock = tools.length
    ? `tools:\n${tools.map((t) => `  - ${t}`).join("\n")}\n`
    : "";
  const skillsBlock = skills.length
    ? `skills:\n${skills.map((s) => `  - ${s}`).join("\n")}\n`
    : "";
  return `schema_version: "1.0"
identity:
  name: ${opts.name}
  role: ${opts.role}
  background: |
    Test persona for Spec 28 rich-output Playwright operator-pass.
  language_default: en
  constraints:
    - Be concise. Use the requested tool directly without asking for confirmation.
self_facts:
  - fact: Test persona.
    confidence: 1.0
worldview:
  - claim: Spec 28 delivers rich output.
    domain: testing
    epistemic: belief
    confidence: 0.95
    valid_time: always
${toolsBlock}${skillsBlock}`;
}

async function seedPersona(
  page: Page,
  token: string,
  opts: SeedOptions,
): Promise<string> {
  const res = await page.request.post(`${API}/v1/personas`, {
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    data: { yaml: personaYaml(opts) },
  });
  expect(res.status(), `seed persona ${opts.name}`).toBe(201);
  return (await res.json()).id as string;
}

async function seedConversation(
  page: Page,
  token: string,
  personaId: string,
): Promise<string> {
  const res = await page.request.post(
    `${API}/v1/personas/${personaId}/conversations`,
    {
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
      },
      data: { title: "" },
    },
  );
  expect(res.status(), "seed conversation").toBe(201);
  return (await res.json()).id as string;
}

async function recordEvidence(page: Page, filename: string): Promise<void> {
  try {
    mkdirSync(EVIDENCE_DIR, { recursive: true });
    await page.screenshot({
      path: join(EVIDENCE_DIR, filename),
      fullPage: true,
    });
  } catch {
    // best-effort
  }
}

async function send(page: Page, prompt: string): Promise<void> {
  await page.getByRole("textbox").fill(prompt);
  await page.getByRole("button", { name: "Send" }).click();
}

test.describe("Spec 28 — Rich Tool Output Delivery (live operator-pass)", () => {
  test("J2 — file_write → FileCard → panel renders Markdown + raw toggle", async ({
    page,
  }) => {
    test.setTimeout(180_000);
    await page.goto("/personas");
    const token = await clerkToken(page);
    const personaId = await seedPersona(page, token, {
      name: "Filewriter",
      role: "File author",
      tools: ["file_write"],
    });
    const conversationId = await seedConversation(page, token, personaId);

    await page.goto(`/chat/${conversationId}`);
    await send(
      page,
      "Use the file_write tool to save a file named 'plan.md' (relative path out/plan.md) containing a short Markdown document with a '# Launch Plan' heading and a two-item bullet list.",
    );

    const card = page.locator('[data-slot="file-card"]').first();
    await expect(card).toBeVisible({ timeout: 120_000 });
    await recordEvidence(page, "j2-file-card.png");

    await card.click();
    const panel = page.locator('[data-slot="file-renderer-panel"]');
    await expect(panel).toBeVisible({ timeout: 15_000 });
    await expect(panel.locator('[data-slot="renderer-markdown"]')).toBeVisible({
      timeout: 15_000,
    });
    await recordEvidence(page, "j2-panel-markdown-rendered.png");

    // Toggle to raw/source.
    await panel.getByRole("button", { name: "Show source" }).click();
    await expect(panel.locator('[data-slot="renderer-raw"]')).toBeVisible({
      timeout: 10_000,
    });
    await recordEvidence(page, "j2-panel-raw.png");

    // Esc closes.
    await page.keyboard.press("Escape");
    await expect(panel).not.toBeVisible({ timeout: 10_000 });
  });

  test("J3 — render_diagram (mermaid) → inline SVG + FileCard + panel", async ({
    page,
  }) => {
    test.setTimeout(180_000);
    await page.goto("/personas");
    const token = await clerkToken(page);
    const personaId = await seedPersona(page, token, {
      name: "Diagrammer",
      role: "Diagram author",
      tools: ["render_diagram"],
    });
    const conversationId = await seedConversation(page, token, personaId);

    await page.goto(`/chat/${conversationId}`);
    await send(
      page,
      "Use the render_diagram tool with format='mermaid' to render this flowchart: graph TD; Idea-->Build; Build-->Ship.",
    );

    const card = page.locator('[data-slot="file-card"]').first();
    await expect(card).toBeVisible({ timeout: 120_000 });
    // Inline SVG affordance (small mermaid source < 100 KB) renders above the card.
    await expect(
      page.locator('[data-slot="renderer-mermaid"] svg').first(),
    ).toBeVisible({
      timeout: 30_000,
    });
    await recordEvidence(page, "j3-mermaid-inline.png");

    await card.click();
    await expect(page.locator('[data-slot="file-renderer-panel"]')).toBeVisible(
      { timeout: 15_000 },
    );
    await recordEvidence(page, "j3-mermaid-panel.png");
  });

  test("J4 — render_diagram (dot) → FileCard → panel", async ({ page }) => {
    test.setTimeout(180_000);
    await page.goto("/personas");
    const token = await clerkToken(page);
    const personaId = await seedPersona(page, token, {
      name: "Dotter",
      role: "Graphviz author",
      tools: ["render_diagram"],
    });
    const conversationId = await seedConversation(page, token, personaId);

    await page.goto(`/chat/${conversationId}`);
    await send(
      page,
      "Use the render_diagram tool with format='dot' to render this Graphviz DOT graph: digraph { a -> b; b -> c }.",
    );

    const card = page.locator('[data-slot="file-card"]').first();
    await expect(card).toBeVisible({ timeout: 120_000 });
    await card.click();
    const panel = page.locator('[data-slot="file-renderer-panel"]');
    await expect(panel).toBeVisible({ timeout: 15_000 });
    await expect(
      panel.locator('[data-slot="renderer-graphviz"] svg').first(),
    ).toBeVisible({
      timeout: 30_000,
    });
    await recordEvidence(page, "j4-graphviz-panel.png");
  });

  test("J1 — generate_image → inline thumbnail + FileCard + panel", async ({
    page,
  }) => {
    test.setTimeout(180_000);
    await page.goto("/personas");
    const token = await clerkToken(page);

    const probe = await page.request.post(
      `${API}/v1/personas/persona_00000000000000000000000000000000/imagegen`,
      {
        headers: { Authorization: `Bearer ${token}` },
        data: { prompt: "test" },
        failOnStatusCode: false,
      },
    );
    if (probe.status() === 503) {
      test.skip(true, "🟦 PARTIAL — image backend not configured (503)");
    }

    const personaId = await seedPersona(page, token, {
      name: "Imagina28",
      role: "Image generator",
      tools: ["generate_image"],
    });
    const conversationId = await seedConversation(page, token, personaId);

    await page.goto(`/chat/${conversationId}`);
    await send(
      page,
      "Use the generate_image tool to generate a minimalist line drawing of a sailboat.",
    );

    const card = page.locator('[data-slot="file-card"]').first();
    await expect(card).toBeVisible({ timeout: 150_000 });
    await recordEvidence(page, "j1-image-card.png");

    await card.click();
    await expect(page.locator('[data-slot="file-renderer-panel"]')).toBeVisible(
      { timeout: 15_000 },
    );
    await expect(
      page
        .locator(
          '[data-slot="file-renderer-panel"] [data-slot="renderer-image"] img',
        )
        .first(),
    ).toHaveAttribute("src", /^blob:/, { timeout: 20_000 });
    await recordEvidence(page, "j1-image-panel.png");
  });

  test("J10 — no-artifact tool renders normally (criterion #9, no empty FileCard)", async ({
    page,
  }) => {
    test.setTimeout(120_000);
    await page.goto("/personas");
    const token = await clerkToken(page);
    const personaId = await seedPersona(page, token, {
      name: "Calc",
      role: "Calculator",
      tools: ["calculator"],
    });
    const conversationId = await seedConversation(page, token, personaId);

    await page.goto(`/chat/${conversationId}`);
    await send(page, "Use the calculator tool to compute 17 * 23.");

    // The persona answers; NO FileCard appears (no artifact produced).
    await expect(page.getByText("391")).toBeVisible({ timeout: 90_000 });
    await expect(page.locator('[data-slot="file-card"]')).toHaveCount(0);
    await recordEvidence(page, "j10-no-filecard.png");
  });
});
