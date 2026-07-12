import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiError } from "@/lib/api/client";
import { fetchPersonaTts } from "./tts";

/**
 * jsdom's `Response`-constructed-from-`Blob` body reading is flaky (the
 * `.blob()` polyfill doesn't fully implement `.stream()`) — the established
 * house workaround (`use-authed-image-blob-url.test.tsx`) is to build the
 * Response with a null body and patch `.blob()` directly.
 */
function blobResponse(blob: Blob, init?: ResponseInit): Response {
  const res = new Response(null, init);
  Object.defineProperty(res, "blob", { value: () => Promise.resolve(blob) });
  return res;
}

describe("fetchPersonaTts", () => {
  const realFetch = global.fetch;
  afterEach(() => {
    global.fetch = realFetch;
  });

  it("POSTs to /v1/personas/:id/tts with the Bearer token + JSON body, returns the audio Blob", async () => {
    let url = "";
    let method = "";
    let auth: string | undefined;
    let contentType: string | undefined;
    let body: string | undefined;
    global.fetch = vi.fn(async (u: RequestInfo | URL, init?: RequestInit) => {
      url = String(u);
      method = init?.method ?? "";
      const headers = init?.headers as Record<string, string> | undefined;
      auth = headers?.Authorization;
      contentType = headers?.["Content-Type"];
      body = init?.body as string | undefined;
      return blobResponse(new Blob([new Uint8Array([1, 2, 3])]), {
        status: 200,
        headers: { "content-type": "audio/wav" },
      });
    }) as unknown as typeof fetch;

    const blob = await fetchPersonaTts("persona_astrid", "Hello there", {
      getToken: async () => "jwt-x",
    });

    expect(url).toBe("http://localhost:8000/v1/personas/persona_astrid/tts");
    expect(method).toBe("POST");
    expect(auth).toBe("Bearer jwt-x");
    expect(contentType).toBe("application/json");
    expect(JSON.parse(body ?? "{}")).toEqual({ text: "Hello there" });
    expect(blob).toBeInstanceOf(Blob);
    expect(blob.size).toBe(3);
  });

  it("URL-encodes the persona id", async () => {
    let url = "";
    global.fetch = vi.fn(async (u: RequestInfo | URL) => {
      url = String(u);
      return blobResponse(new Blob([]), { status: 200 });
    }) as unknown as typeof fetch;

    await fetchPersonaTts("persona/weird id", "hi", {
      getToken: async () => "x",
    });
    expect(url).toContain(encodeURIComponent("persona/weird id"));
  });

  it("omits the Authorization header when there is no token", async () => {
    let auth: string | undefined = "unset";
    global.fetch = vi.fn(async (_u: RequestInfo | URL, init?: RequestInit) => {
      const headers = init?.headers as Record<string, string> | undefined;
      auth = headers?.Authorization;
      return blobResponse(new Blob([]), { status: 200 });
    }) as unknown as typeof fetch;

    await fetchPersonaTts("p1", "hi", { getToken: async () => null });
    expect(auth).toBeUndefined();
  });

  it("throws ApiError with the parsed body on a non-2xx", async () => {
    global.fetch = vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            error: "voice_unavailable",
            detail: "not configured",
          }),
          { status: 503 },
        ),
    ) as unknown as typeof fetch;

    await expect(
      fetchPersonaTts("p1", "hi", { getToken: async () => "x" }),
    ).rejects.toMatchObject({
      status: 503,
      code: "voice_unavailable",
    } satisfies Partial<ApiError>);
  });

  it("throws ApiError on 413 (text too long)", async () => {
    global.fetch = vi.fn(
      async () =>
        new Response(JSON.stringify({ error: "tts_text_too_long" }), {
          status: 413,
        }),
    ) as unknown as typeof fetch;

    await expect(
      fetchPersonaTts("p1", "x".repeat(5000), { getToken: async () => "x" }),
    ).rejects.toMatchObject({ status: 413 } satisfies Partial<ApiError>);
  });
});
