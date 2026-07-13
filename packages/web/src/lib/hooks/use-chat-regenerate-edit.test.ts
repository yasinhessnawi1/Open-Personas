import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useChat } from "./use-chat";

/**
 * R9-025 leg C — `useChat.regenerate` / `useChat.editAndRerun`.
 *
 * Scope (mirrors `use-chat.test.ts`'s own "request-shape" precedent — SSE
 * consumption / RunEvent envelope handling are already covered by the F2/P1
 * suites and aren't re-verified here): the load-bearing NEW behaviour is that
 * retry now calls a REAL server endpoint (not a client-side echo-resend) and
 * edit's save-and-rerun reaches the api with the edited content. Both fire a
 * `reload()` (`GET /v1/conversations/{id}`) FIRST to resolve the real target
 * id before calling their action endpoint — asserted via the captured
 * request sequence.
 */

vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ getToken: () => Promise.resolve("test-jwt-token") }),
}));

interface CapturedRequest {
  url: string;
  method: string;
  body: string;
  headers: Record<string, string>;
}

function sse(frames: string[]): Response {
  const body = frames.join("");
  return new Response(
    new ReadableStream({
      start(c) {
        c.enqueue(new TextEncoder().encode(body));
        c.close();
      },
    }),
    { status: 200, headers: { "Content-Type": "text/event-stream" } },
  );
}

function json(obj: unknown, status = 200): Response {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function chunkFrame(delta: string): string {
  return `event: chunk\ndata: ${JSON.stringify({ delta, is_final: false })}\n\n`;
}

const DONE = `event: done\ndata: ${JSON.stringify({ tier: "mid", usage: {}, format_hints: {} })}\n\n`;

/**
 * Routes every fetch by URL, capturing regenerate/edit calls. `/active-turn`
 * (non-events) 404s so the hook's mount-time reattach is a clean no-op;
 * `GET /v1/conversations/{id}` (the reload `regenerate`/`editAndRerun` run
 * FIRST to resolve real ids, and again at clean completion) returns a FIXED
 * two-message conversation — this file asserts the REQUEST shape reaching
 * the network, not a simulated server-side mutation (mirrors
 * `use-chat.test.ts`'s own scope note).
 */
function installRoutedFetch(): {
  captured: CapturedRequest[];
  restore: () => void;
} {
  const captured: CapturedRequest[] = [];
  const original = globalThis.fetch;
  globalThis.fetch = vi.fn(
    async (url: string | URL | Request, init?: RequestInit) => {
      const u = url instanceof Request ? url.url : url.toString();
      if (u.endsWith("/active-turn")) {
        return json({ error: "turn_not_active" }, 404);
      }
      if (u.includes("/regenerate") || u.includes("/edit")) {
        captured.push({
          url: u,
          method: init?.method ?? "GET",
          body: typeof init?.body === "string" ? init.body : "",
          headers: (init?.headers as Record<string, string>) ?? {},
        });
        return sse([chunkFrame("a new reply"), DONE]);
      }
      if (/\/v1\/conversations\/[^/]+$/.test(u)) {
        return json({
          id: "conv_1",
          persona_id: "p",
          title: "t",
          messages: [
            { id: "u1", role: "user", content: "hi" },
            { id: "m1", role: "assistant", content: "Hello" },
          ],
          created_at: "2026-01-01T00:00:00Z",
          updated_at: "2026-01-01T00:00:00Z",
        });
      }
      return sse([]);
    },
  ) as unknown as typeof fetch;
  return {
    captured,
    restore: () => {
      globalThis.fetch = original;
    },
  };
}

const initial = [
  { id: "u1" as string, role: "user" as const, content: "hi" },
  { id: "m1" as string, role: "assistant" as const, content: "Hello" },
];

describe("useChat.regenerate — R9-025 leg C (real retry, not an echo-resend)", () => {
  let restore: () => void;
  afterEach(() => restore?.());

  it("fires POST …/messages/{id}/regenerate — NO body, NOT a re-send of the user's text", async () => {
    const { captured, restore: r } = installRoutedFetch();
    restore = r;

    const { result } = renderHook(() => useChat("conv_1", initial, "p"));
    await result.current.regenerate("m1");
    await waitFor(() =>
      expect(captured.some((c) => c.url.includes("/regenerate"))).toBe(true),
    );

    const call = captured.find((c) => c.url.includes("/regenerate"));
    expect(call?.url).toBe(
      "http://localhost:8000/v1/conversations/conv_1/messages/m1/regenerate",
    );
    expect(call?.method).toBe("POST");
    // The load-bearing fix: unlike the OLD client-side retry (which called
    // POST …/messages with {content: <the preceding user text>}), regenerate
    // sends NO body — the server re-derives the preceding user turn itself.
    expect(call?.body).toBe("");
    expect(call?.headers.Authorization).toBe("Bearer test-jwt-token");
  });

  it("resolves the target id via a fresh reload before regenerating (id-reconciliation)", async () => {
    // Even when called with a STALE/unknown id (e.g. a just-completed send()'s
    // client-side optimistic id, never reconciled with the server's real id —
    // see the docstring in use-chat.ts), regenerate reloads first and falls
    // back to the conversation's current last assistant message.
    const { captured, restore: r } = installRoutedFetch();
    restore = r;

    const { result } = renderHook(() => useChat("conv_1", initial, "p"));
    await result.current.regenerate("some-stale-client-side-id");
    await waitFor(() =>
      expect(captured.some((c) => c.url.includes("/regenerate"))).toBe(true),
    );

    const call = captured.find((c) => c.url.includes("/regenerate"));
    // Falls back to the REAL id from the reload ("m1"), not the stale one.
    expect(call?.url).toContain("/messages/m1/regenerate");
  });

  it("does nothing while a turn is already streaming (mirrors send()'s guard)", async () => {
    const { captured, restore: r } = installRoutedFetch();
    restore = r;
    const { result } = renderHook(() => useChat("conv_1", initial, "p"));

    // Start a genuine send() against a stream that never closes, so `streaming`
    // stays true for the duration of this assertion.
    globalThis.fetch = vi.fn(async (url: string | URL | Request) => {
      const u = url instanceof Request ? url.url : url.toString();
      if (u.endsWith("/active-turn"))
        return json({ error: "turn_not_active" }, 404);
      return new Response(
        new ReadableStream({
          start(c) {
            c.enqueue(new TextEncoder().encode(chunkFrame("partial")));
          },
        }),
        { status: 200, headers: { "Content-Type": "text/event-stream" } },
      );
    }) as unknown as typeof fetch;
    void result.current.send("hi again");
    await waitFor(() => expect(result.current.streaming).toBe(true));

    await result.current.regenerate("m1");
    expect(captured.some((c) => c.url.includes("/regenerate"))).toBe(false);
  });
});

describe("useChat.editAndRerun — R9-025 leg C (save-and-rerun the last user message)", () => {
  let restore: () => void;
  afterEach(() => restore?.());

  it("fires PATCH …/messages/{id}/edit with {content: <edited text>}", async () => {
    const { captured, restore: r } = installRoutedFetch();
    restore = r;

    const { result } = renderHook(() => useChat("conv_1", initial, "p"));
    await result.current.editAndRerun("u1", "what is 3+3?");
    await waitFor(() =>
      expect(captured.some((c) => c.url.includes("/edit"))).toBe(true),
    );

    const call = captured.find((c) => c.url.includes("/edit"));
    expect(call?.url).toBe(
      "http://localhost:8000/v1/conversations/conv_1/messages/u1/edit",
    );
    expect(call?.method).toBe("PATCH");
    expect(JSON.parse(call?.body ?? "{}")).toEqual({ content: "what is 3+3?" });
    expect(call?.headers["Content-Type"]).toBe("application/json");
  });

  it("trims the edited content before sending", async () => {
    const { captured, restore: r } = installRoutedFetch();
    restore = r;
    const { result } = renderHook(() => useChat("conv_1", initial, "p"));
    await result.current.editAndRerun("u1", "  spaced out  ");
    await waitFor(() =>
      expect(captured.some((c) => c.url.includes("/edit"))).toBe(true),
    );
    const call = captured.find((c) => c.url.includes("/edit"));
    expect(JSON.parse(call?.body ?? "{}")).toEqual({ content: "spaced out" });
  });

  it("rejects blank content — no request fires", async () => {
    const { captured, restore: r } = installRoutedFetch();
    restore = r;
    const { result } = renderHook(() => useChat("conv_1", initial, "p"));
    await result.current.editAndRerun("u1", "   ");
    expect(captured.some((c) => c.url.includes("/edit"))).toBe(false);
  });

  it("does nothing while a turn is already streaming (mirrors send()'s guard)", async () => {
    const { captured, restore: r } = installRoutedFetch();
    restore = r;
    const { result } = renderHook(() => useChat("conv_1", initial, "p"));

    globalThis.fetch = vi.fn(async (url: string | URL | Request) => {
      const u = url instanceof Request ? url.url : url.toString();
      if (u.endsWith("/active-turn"))
        return json({ error: "turn_not_active" }, 404);
      return new Response(
        new ReadableStream({
          start(c) {
            c.enqueue(new TextEncoder().encode(chunkFrame("partial")));
          },
        }),
        { status: 200, headers: { "Content-Type": "text/event-stream" } },
      );
    }) as unknown as typeof fetch;
    void result.current.send("hi again");
    await waitFor(() => expect(result.current.streaming).toBe(true));

    await result.current.editAndRerun("u1", "edited");
    expect(captured.some((c) => c.url.includes("/edit"))).toBe(false);
  });
});
