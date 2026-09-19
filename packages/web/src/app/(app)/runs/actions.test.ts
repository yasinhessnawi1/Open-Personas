/**
 * Issue #16 - the Start-now door reads the attached files back off the form.
 *
 * The dialog posts the landed workspace refs as one hidden field, because a server action
 * receives a FormData and nothing else. If this half were missing, the chips would look
 * right and the persona would still get nothing, which is the exact shape of defect this
 * project keeps finding. The POST body is the assertion.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

const post = vi.hoisted(() => vi.fn());
const redirect = vi.hoisted(() => vi.fn());

vi.mock("next/navigation", () => ({ redirect }));
vi.mock("@/lib/api/server", () => ({
  serverApi: async () => ({ POST: post }),
}));
vi.mock("@/lib/api", () => ({
  unwrap: async (r: unknown) => (await r) as { task_id: string },
}));

import { startTask } from "./actions";

function form(fields: Record<string, string>): FormData {
  const fd = new FormData();
  for (const [k, v] of Object.entries(fields)) fd.append(k, v);
  return fd;
}

const REF = {
  ref: "uploads/9f2c0a.md",
  filename: "brief.md",
  media_type: "text/markdown",
};

beforeEach(() => {
  vi.clearAllMocks();
});

describe("startTask", () => {
  it("sends the attached refs with the task", async () => {
    post.mockResolvedValue({ task_id: "t_new" });
    await startTask(
      form({
        persona_id: "kai",
        task: "summarise these",
        attachments: JSON.stringify([REF]),
      }),
    );
    expect(post.mock.calls[0][1].body).toEqual({
      task: "summarise these",
      attachments: [REF],
    });
    expect(redirect).toHaveBeenCalledWith("/activity/tasks/t_new");
  });

  it("sends an empty list when nothing was attached", async () => {
    post.mockResolvedValue({ task_id: "t2" });
    await startTask(form({ persona_id: "kai", task: "just think" }));
    expect(post.mock.calls[0][1].body).toEqual({
      task: "just think",
      attachments: [],
    });
  });

  it("drops a field it did not write rather than losing the goal", async () => {
    // A truncated or tampered value is not a reason to refuse the task the person typed.
    post.mockResolvedValue({ task_id: "t3" });
    await startTask(
      form({ persona_id: "kai", task: "carry on", attachments: "{not json" }),
    );
    expect(post.mock.calls[0][1].body).toEqual({
      task: "carry on",
      attachments: [],
    });
  });

  it("ignores entries with no ref", async () => {
    post.mockResolvedValue({ task_id: "t4" });
    await startTask(
      form({
        persona_id: "kai",
        task: "carry on",
        attachments: JSON.stringify([{ filename: "ghost.md" }, REF]),
      }),
    );
    expect(post.mock.calls[0][1].body.attachments).toEqual([REF]);
  });
});
