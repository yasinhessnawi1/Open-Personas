import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "./api/client";
import { turnMessageIntoFile } from "./turn-into-file";

describe("turnMessageIntoFile — R9-025b", () => {
  beforeEach(() => {
    globalThis.fetch = vi.fn(
      async () =>
        new Response(
          JSON.stringify({ job_id: "job_abc123", status: "queued" }),
          { status: 202, headers: { "Content-Type": "application/json" } },
        ),
    ) as unknown as typeof fetch;
  });
  afterEach(() => vi.restoreAllMocks());

  it("POSTs to the correct path with Bearer auth + the format body", async () => {
    let capturedUrl = "";
    let capturedMethod: string | undefined;
    let capturedAuth: string | undefined;
    let capturedBody: string | undefined;
    globalThis.fetch = vi.fn(async (url, init) => {
      const req = url instanceof Request ? url : null;
      capturedUrl = req?.url ?? String(url);
      capturedMethod = req?.method ?? (init?.method as string | undefined);
      capturedAuth =
        req?.headers.get("Authorization") ??
        (init?.headers as Record<string, string> | undefined)?.Authorization;
      capturedBody = req
        ? await req.clone().text()
        : (init?.body as string | undefined);
      return new Response(
        JSON.stringify({ job_id: "job_abc123", status: "queued" }),
        { status: 202, headers: { "Content-Type": "application/json" } },
      );
    }) as unknown as typeof fetch;

    const result = await turnMessageIntoFile(
      "conv_1",
      "msg_1",
      "pdf",
      async () => "jwt-x",
    );

    expect(capturedUrl).toBe(
      "http://localhost:8000/v1/conversations/conv_1/messages/msg_1/turn-into-file",
    );
    expect(capturedMethod).toBe("POST");
    expect(capturedAuth).toBe("Bearer jwt-x");
    expect(JSON.parse(capturedBody ?? "{}")).toEqual({ format: "pdf" });
    expect(result).toEqual({ job_id: "job_abc123", status: "queued" });
  });

  it("defaults to auto when no format override is given elsewhere (caller passes it explicitly)", async () => {
    await turnMessageIntoFile("conv_1", "msg_1", "auto", async () => "jwt-x");
    // No throw — the 202 scripted response resolves.
  });

  it("a duplicate-enqueue response (job_id null) resolves, not throws", async () => {
    globalThis.fetch = vi.fn(
      async () =>
        new Response(JSON.stringify({ job_id: null, status: "queued" }), {
          status: 202,
          headers: { "Content-Type": "application/json" },
        }),
    ) as unknown as typeof fetch;
    const result = await turnMessageIntoFile(
      "conv_1",
      "msg_1",
      "auto",
      async () => "jwt-x",
    );
    expect(result.job_id).toBeNull();
  });

  it("422 (wrong message role) throws ApiError so the caller can toast", async () => {
    globalThis.fetch = vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            error: "wrong_message_role",
            detail: { reason: "only assistant" },
          }),
          { status: 422, headers: { "Content-Type": "application/json" } },
        ),
    ) as unknown as typeof fetch;
    await expect(
      turnMessageIntoFile("conv_1", "msg_1", "auto", async () => "jwt-x"),
    ).rejects.toThrow(ApiError);
  });

  it("503 (feature unavailable) throws ApiError", async () => {
    globalThis.fetch = vi.fn(
      async () =>
        new Response(JSON.stringify({ error: "file_extract_unavailable" }), {
          status: 503,
          headers: { "Content-Type": "application/json" },
        }),
    ) as unknown as typeof fetch;
    await expect(
      turnMessageIntoFile("conv_1", "msg_1", "auto", async () => "jwt-x"),
    ).rejects.toThrow(ApiError);
  });
});
