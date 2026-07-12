import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiError } from "@/lib/api/client";
import { transcribeAudio } from "./stt";

describe("transcribeAudio", () => {
  const realFetch = global.fetch;
  afterEach(() => {
    global.fetch = realFetch;
  });

  it("POSTs multipart audio to /v1/stt with the Bearer token, returns the transcript", async () => {
    let url = "";
    let method = "";
    let auth: string | undefined;
    let contentTypeHeaderSetExplicitly = false;
    let receivedForm: FormData | undefined;
    global.fetch = vi.fn(async (u: RequestInfo | URL, init?: RequestInit) => {
      url = String(u);
      method = init?.method ?? "";
      const headers = init?.headers as Record<string, string> | undefined;
      auth = headers?.Authorization;
      contentTypeHeaderSetExplicitly = !!headers?.["Content-Type"];
      receivedForm = init?.body as FormData;
      return new Response(JSON.stringify({ transcript: "buy milk tomorrow" }), {
        status: 200,
      });
    }) as unknown as typeof fetch;

    const audio = new Blob([new Uint8Array([1, 2, 3])], { type: "audio/webm" });
    const transcript = await transcribeAudio(audio, {
      getToken: async () => "jwt-y",
    });

    expect(url).toBe("http://localhost:8000/v1/stt");
    expect(method).toBe("POST");
    expect(auth).toBe("Bearer jwt-y");
    // The Content-Type (multipart boundary) must be left to the browser —
    // setting it manually would break the upload.
    expect(contentTypeHeaderSetExplicitly).toBe(false);
    expect(receivedForm).toBeInstanceOf(FormData);
    expect(receivedForm?.get("audio")).toBeInstanceOf(Blob);
    expect(transcript).toBe("buy milk tomorrow");
  });

  it("returns an empty string when the server omits transcript (defensive)", async () => {
    global.fetch = vi.fn(
      async () => new Response(JSON.stringify({}), { status: 200 }),
    ) as unknown as typeof fetch;

    const transcript = await transcribeAudio(new Blob([]), {
      getToken: async () => "x",
    });
    expect(transcript).toBe("");
  });

  it("omits the Authorization header when there is no token", async () => {
    let auth: string | undefined = "unset";
    global.fetch = vi.fn(async (_u: RequestInfo | URL, init?: RequestInit) => {
      const headers = init?.headers as Record<string, string> | undefined;
      auth = headers?.Authorization;
      return new Response(JSON.stringify({ transcript: "" }), { status: 200 });
    }) as unknown as typeof fetch;

    await transcribeAudio(new Blob([]), { getToken: async () => null });
    expect(auth).toBeUndefined();
  });

  it("throws ApiError with the parsed body on a non-2xx", async () => {
    global.fetch = vi.fn(
      async () =>
        new Response(JSON.stringify({ error: "voice_unavailable" }), {
          status: 503,
        }),
    ) as unknown as typeof fetch;

    await expect(
      transcribeAudio(new Blob([]), { getToken: async () => "x" }),
    ).rejects.toMatchObject({
      status: 503,
      code: "voice_unavailable",
    } satisfies Partial<ApiError>);
  });

  it("throws ApiError on 413 (audio too large)", async () => {
    global.fetch = vi.fn(
      async () =>
        new Response(JSON.stringify({ error: "stt_audio_too_large" }), {
          status: 413,
        }),
    ) as unknown as typeof fetch;

    await expect(
      transcribeAudio(new Blob([new Uint8Array(10)]), {
        getToken: async () => "x",
      }),
    ).rejects.toMatchObject({ status: 413 } satisfies Partial<ApiError>);
  });
});
