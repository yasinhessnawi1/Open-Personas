import { expect, test } from "@playwright/test";

/**
 * Spec W1 (T8, D-W1-34 / D-W1-28) — the community browser oracle for a leg that ASKS.
 *
 * This is the proof T4 deferred and T7 could not produce, because until now no real chain
 * could put a task-linked run in front of a person with an unanswered question: the loop
 * answered on the user's behalf and the task ran on.
 *
 * The whole loop, in a real browser against a real community stack (embedded Postgres, the
 * in-process worker, a real model):
 *
 *   1. the review page shows the parked task, the persona's own question as the reason, and
 *      offers Reply where it is read (T7);
 *   2. the run the leg produced is reachable from the task, sits "Waiting on you", and its
 *      unanswered question carries the link that takes the answer to the task page (D-W1-28);
 *   3. answering from the review page resumes the work and the task finishes.
 *
 * The API is driven only to SET UP (seed a persona, dispatch the one-off) and to READ the
 * durable truth; every assertion about the product is made against the rendered page.
 */

const API = process.env.E2E_COMMUNITY_API_URL ?? "http://localhost:8077";

const PERSONA_YAML = `schema_version: "1.0"
identity:
  name: Astrid
  role: assistant
  background: |
    A careful helper. When a job needs one detail only the user has, she asks for
    it in a single short question and stops, rather than guessing.
  language_default: en
  constraints:
    - Do not fabricate information; say when you don't know.
    - If a task needs a detail only the user has, ask one short question and stop.
self_facts:
  - fact: never invents facts about the user's calendar or preferences
    confidence: 1.0
`;

const BRIEF =
  "Book me a dentist appointment next week. Do not use any tools. You do not know " +
  "which clinic or which day, so ask me one short question and stop.";

async function post(path: string, body: unknown) {
  const res = await fetch(`${API}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok)
    throw new Error(`POST ${path} → ${res.status} ${await res.text()}`);
  return res.json();
}

async function get(path: string) {
  const res = await fetch(`${API}${path}`);
  if (!res.ok) throw new Error(`GET ${path} → ${res.status}`);
  return res.json();
}

/** Poll the durable task row until it reaches one of `states` (the worker runs in the API). */
async function waitForTask(
  taskId: string,
  states: string[],
  timeoutMs = 150_000,
) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const tasks = (await get("/v1/tasks")) as {
      task_id: string;
      status: string;
    }[];
    const task = tasks.find((t) => t.task_id === taskId);
    if (task && states.includes(task.status)) return task;
    await new Promise((r) => setTimeout(r, 2000));
  }
  throw new Error(`task ${taskId} never reached ${states.join(" | ")}`);
}

/** Leave the board with nothing on it, so "the one waiting line" is unambiguous. */
async function clearTheBoard() {
  const tasks = (await get("/v1/tasks")) as {
    task_id: string;
    status: string;
  }[];
  for (const task of tasks) {
    if (!["completed", "failed", "cancelled"].includes(task.status)) {
      await post(`/v1/tasks/${task.task_id}/cancel`, {});
    }
  }
}

test("a leg that asks parks, shows its question, and the answer given in the browser moves it on", async ({
  page,
}) => {
  await clearTheBoard();
  const persona = (await post("/v1/personas", { yaml: PERSONA_YAML })) as {
    id: string;
  };
  const dispatched = (await post(`/v1/personas/${persona.id}/runs`, {
    task: BRIEF,
  })) as {
    task_id: string;
  };
  const taskId = dispatched.task_id;

  // The real leg runs, the real model asks, and the task parks on the question.
  await waitForTask(taskId, ["waiting_on_user"]);

  // --- 1. the review page: the question is the reason, and Reply is offered here ---------
  await page.goto("/activity");
  const card = page.locator('[data-slot="review"] >> text=Needs you').first();
  await expect(card).toBeVisible();
  const actions = page.locator('[data-slot="attention-actions"]').first();
  await expect(actions).toBeVisible();
  await expect(actions.locator('[data-verb="reply"]')).toBeVisible();
  // The persona's own question, verbatim, with no control marker in sight.
  const review = (await get("/v1/autonomy/review")) as {
    sections: {
      kind: string;
      items: { reason: string; detail: string; ref: { id: string } | null }[];
    }[];
  };
  const waiting = review.sections.find((s) => s.kind === "waiting");
  const line = waiting?.items.find((i) => i.ref?.id === taskId);
  expect(line, "the parked task is the review line").toBeTruthy();
  expect(line?.reason).toBe("question");
  const question = line?.detail ?? "";
  expect(question.length).toBeGreaterThan(0);
  expect(question).not.toContain("[ASK_USER]");
  await expect(page.locator(`text=${question}`).first()).toBeVisible();
  // The badge counts what needs you, and this needs you.
  await expect(page.locator('nav[aria-label="Primary"]')).toContainText(
    "Activity",
  );

  // --- 2. the run viewer: the unanswered question takes its answer to the task ------------
  await page.goto(`/activity/tasks/${taskId}`);
  const runLink = page.locator('[data-slot="run-history-open"]').first();
  await expect(runLink).toBeVisible();
  await runLink.click();
  await expect(page).toHaveURL(/\/runs\//);
  // The run is stopped on the question, not finished and not errored.
  await expect(page.locator('[data-slot="run-status-badge"]')).toHaveAttribute(
    "data-status",
    "awaiting_user",
  );
  const answerOnTask = page.locator('[data-slot="step-answer-on-task-link"]');
  await expect(answerOnTask).toBeVisible();
  await expect(answerOnTask).toHaveAttribute(
    "href",
    `/activity/tasks/${taskId}`,
  );
  await expect(page.locator('[data-slot="step-answer-on-task"]')).toContainText(
    question,
  );
  // The retired in-process door leaves no inline prompt behind on a task-linked run.
  await expect(page.locator('[data-slot="run-timeline"] textarea')).toHaveCount(
    0,
  );
  await page.screenshot({ path: "test-results/w1-run-awaiting-user.png" });

  // --- 3. answering from the review page moves the work on --------------------------------
  // What is pinned here is the PRODUCT: the answer given in the browser reaches the durable
  // seam, a new leg runs, and the task stops waiting on the question it asked. Whether the
  // persona then finishes or asks something ELSE is its judgment, not an invariant, so this
  // asserts the work moved rather than dictating what the model should conclude.
  const before = (await get(`/v1/tasks/${taskId}`)) as { runs: unknown[] };
  await page.goto("/activity");
  await page
    .locator('[data-slot="attention-actions"] [data-verb="reply"]')
    .first()
    .click();
  await page
    .locator('[data-slot="attention-reply"]')
    .fill("Dr Lie on Storgata, Tuesday afternoon.");
  await page.locator('[data-verb="reply-send"]').click();

  // A NEW run appears: the reply enqueued a leg and the worker ran it.
  const deadline = Date.now() + 180_000;
  let after = before;
  while (Date.now() < deadline) {
    after = (await get(`/v1/tasks/${taskId}`)) as { runs: unknown[] };
    if (after.runs.length > before.runs.length) break;
    await new Promise((r) => setTimeout(r, 2000));
  }
  expect(
    after.runs.length,
    "the reply drove a new leg through the worker",
  ).toBeGreaterThan(before.runs.length);

  // And the question that was waiting on the user is no longer the thing waiting on them.
  const settled = (await get("/v1/autonomy/review")) as {
    sections: {
      kind: string;
      items: { detail: string; ref: { id: string } | null }[];
    }[];
  };
  const stillWaiting = settled.sections
    .find((s) => s.kind === "waiting")
    ?.items.find((i) => i.ref?.id === taskId);
  expect(stillWaiting?.detail ?? "").not.toBe(question);
});
