/**
 * Spec W1 (T8 fold-in) — the task commands hit their OWN route, with their own method and body.
 *
 * The component tests mock these functions, so they pin that Pick up calls `pickupTask` and
 * Reply calls `replyToTask`, but nothing underneath: a mutation that pointed `pickupTask` at
 * the `/reply` route left every one of them green. The user would press Pick up and get a 422
 * for a missing reply body, or worse, a reply with no words in it.
 *
 * So this drives the real client functions against a mocked `fetch` and asserts the wire:
 * path, method, and for a reply the body that carries the user's own words.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  cancelTask,
  extendBudget,
  pauseTask,
  pickupTask,
  replyToTask,
  resumeTask,
  retryTask,
} from "./tasks-client";

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  // A fresh Response per call: a body can only be read once, and these tests make several.
  fetchMock.mockImplementation(() =>
    Promise.resolve(
      new Response(JSON.stringify({ task_id: "t1", changed: true }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    ),
  );
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => vi.unstubAllGlobals());

/** The one call the function under test made: its URL, method and parsed body. */
function sent() {
  expect(fetchMock).toHaveBeenCalledOnce();
  const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
  return {
    path: new URL(url).pathname,
    method: init.method,
    body: init.body ? JSON.parse(String(init.body)) : undefined,
    auth: new Headers(init.headers).get("Authorization"),
  };
}

describe("each task command posts to its own route", () => {
  it("pickup goes to /pickup with no body", async () => {
    await pickupTask("tok", "t1");
    expect(sent()).toMatchObject({
      path: "/v1/tasks/t1/pickup",
      method: "POST",
      body: undefined,
    });
  });

  it("reply goes to /reply and carries the user's words", async () => {
    await replyToTask("tok", "t1", "the 14th works");
    expect(sent()).toMatchObject({
      path: "/v1/tasks/t1/reply",
      method: "POST",
      body: { reply: "the 14th works" },
    });
  });

  it("retry goes to /retry with no body", async () => {
    await retryTask("tok", "t1");
    expect(sent()).toMatchObject({
      path: "/v1/tasks/t1/retry",
      method: "POST",
      body: undefined,
    });
  });

  it("pause, resume and cancel each go to their own verb", async () => {
    for (const [fn, verb] of [
      [pauseTask, "pause"],
      [resumeTask, "resume"],
      [cancelTask, "cancel"],
    ] as const) {
      fetchMock.mockClear();
      await fn("tok", "t1");
      expect(sent()).toMatchObject({
        path: `/v1/tasks/t1/${verb}`,
        method: "POST",
      });
    }
  });

  it("budget extension carries the amount", async () => {
    await extendBudget("tok", "t1", 5_000_000);
    expect(sent()).toMatchObject({
      path: "/v1/tasks/t1/budget/extend",
      method: "POST",
      body: { amount_micros: 5_000_000 },
    });
  });
});

describe("the wire's other honest details", () => {
  it("carries the bearer token when there is one, and omits it when there is not", async () => {
    await pickupTask("tok", "t1");
    expect(sent().auth).toBe("Bearer tok");
    fetchMock.mockClear();
    await pickupTask(null, "t1");
    expect(sent().auth).toBeNull();
  });

  it("escapes a task id rather than splicing it into the path raw", async () => {
    await pickupTask("tok", "t 1/../admin");
    expect(sent().path).toBe("/v1/tasks/t%201%2F..%2Fadmin/pickup");
  });

  it("raises on a failed command instead of returning a shape the surface would render", async () => {
    fetchMock.mockImplementation(() =>
      Promise.resolve(new Response("nope", { status: 500 })),
    );
    await expect(pickupTask("tok", "t1")).rejects.toThrow("500");
  });
});
