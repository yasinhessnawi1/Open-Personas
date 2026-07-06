import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "./api/client";
import { consumeSSE, type RawSSEEvent } from "./sse";
import { parseChatEvent, parseRunEvent } from "./sse-types";

function streamOf(chunks: string[]): ReadableStream<Uint8Array> {
  const enc = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const c of chunks) controller.enqueue(enc.encode(c));
      controller.close();
    },
  });
}

function mockOkStream(chunks: string[]) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(streamOf(chunks), { status: 200 })),
  );
}

async function collect(): Promise<RawSSEEvent[]> {
  const out: RawSSEEvent[] = [];
  for await (const ev of consumeSSE("http://x/stream")) out.push(ev);
  return out;
}

afterEach(() => vi.unstubAllGlobals());

describe("consumeSSE", () => {
  it("yields a frame split across multiple reads", async () => {
    // The chunk frame's data is split mid-JSON across two network reads.
    mockOkStream([
      'event: chunk\ndata: {"delta":"He',
      'llo","is_final":false}\n\n',
      "event: done\ndata: {}\n\n",
    ]);
    const events = await collect();
    expect(events).toEqual([
      { event: "chunk", data: '{"delta":"Hello","is_final":false}' },
      { event: "done", data: "{}" },
    ]);
  });

  it("parses multiple frames in a single chunk and strips one leading space", async () => {
    mockOkStream(['event: a\ndata: {"x":1}\n\nevent: b\ndata: {"y":2}\n\n']);
    const events = await collect();
    expect(events).toEqual([
      { event: "a", data: '{"x":1}' },
      { event: "b", data: '{"y":2}' },
    ]);
  });

  it("ignores comments/blank-only frames and flushes a trailing frame", async () => {
    mockOkStream([": keep-alive\n\n", "event: done\ndata: {}"]);
    const events = await collect();
    expect(events).toEqual([{ event: "done", data: "{}" }]);
  });

  it("normalises CRLF line endings", async () => {
    mockOkStream(["event: chunk\r\ndata: {}\r\n\r\n"]);
    const events = await collect();
    expect(events).toEqual([{ event: "chunk", data: "{}" }]);
  });

  it("throws ApiError with the structured body on a non-OK response", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(JSON.stringify({ error: "rate_limit_exceeded" }), {
            status: 429,
            headers: { "Retry-After": "30" },
          }),
      ),
    );
    let caught: unknown;
    try {
      await collect();
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeInstanceOf(ApiError);
    const err = caught as ApiError;
    expect(err.status).toBe(429);
    expect(err.code).toBe("rate_limit_exceeded");
    expect(err.rateLimit.retryAfter).toBe(30);
  });

  it("surfaces the id: field (A11 Last-Event-ID cursor) and omits it when absent", async () => {
    mockOkStream([
      "id: e1:7\nevent: notification.created\ndata: {}\n\n",
      "event: chunk\ndata: {}\n\n",
    ]);
    const events = await collect();
    expect(events[0]).toEqual({
      event: "notification.created",
      data: "{}",
      id: "e1:7",
    });
    expect(events[1]).toEqual({ event: "chunk", data: "{}" }); // no id key
    expect("id" in events[1]).toBe(false);
  });
});

describe("parseChatEvent (bare payload)", () => {
  it("parses chunk / tool_calling / tool_result / done", () => {
    expect(
      parseChatEvent({
        event: "chunk",
        data: '{"delta":"hi","is_final":true}',
      }),
    ).toEqual({
      event: "chunk",
      data: { delta: "hi", is_final: true },
    });
    const tr = parseChatEvent({
      event: "tool_result",
      data: '{"tool_name":"web_search","is_error":false,"content":"ok"}',
    });
    expect(tr).toEqual({
      event: "tool_result",
      data: { tool_name: "web_search", is_error: false, content: "ok" },
    });
    const done = parseChatEvent({
      event: "done",
      data: '{"usage":{"prompt_tokens":1,"completion_tokens":2},"tier":"mid","format_hints":{}}',
    });
    expect(done?.event).toBe("done");
    if (done?.event === "done") expect(done.data.tier).toBe("mid");
  });

  it("returns null for unknown event names", () => {
    expect(
      parseChatEvent({ event: "totally_unknown_event", data: "{}" }),
    ).toBeNull();
  });
});

describe("parseRunEvent (full envelope)", () => {
  it("parses a tool_result envelope with payload under .data", () => {
    const ev = parseRunEvent({
      event: "tool_result",
      data: '{"type":"tool_result","step":0,"data":{"tool_name":"web_search","is_error":false,"content":"x"},"timestamp":"2026-05-29T00:00:00Z"}',
    });
    expect(ev?.type).toBe("tool_result");
    expect(ev?.step).toBe(0);
    if (ev?.type === "tool_result")
      expect(ev.data.tool_name).toBe("web_search");
  });

  it("returns null for the terminal end frame and for malformed frames", () => {
    expect(parseRunEvent({ event: "end", data: "{}" })).toBeNull();
    expect(parseRunEvent({ event: "x", data: "{}" })).toBeNull();
  });
});
