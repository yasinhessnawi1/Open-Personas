import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  createApiClient,
  isAccountErased,
  readRateLimit,
  unwrap,
} from "./client";

function headers(init: Record<string, string>): Headers {
  return new Headers(init);
}

describe("readRateLimit", () => {
  it("parses the rate-limit headers", () => {
    const rl = readRateLimit(
      headers({
        "X-RateLimit-Limit": "20",
        "X-RateLimit-Remaining": "19",
        "X-RateLimit-Reset": "1700000000",
        "Retry-After": "5",
      }),
    );
    expect(rl).toEqual({
      limit: 20,
      remaining: 19,
      reset: 1700000000,
      retryAfter: 5,
    });
  });

  it("returns nulls when headers are absent", () => {
    expect(readRateLimit(headers({}))).toEqual({
      limit: null,
      remaining: null,
      reset: null,
      retryAfter: null,
    });
  });
});

describe("unwrap", () => {
  it("returns data on success", async () => {
    const result = {
      data: { id: "p1" },
      response: new Response(null, { status: 200 }),
    };
    await expect(unwrap(result)).resolves.toEqual({ id: "p1" });
  });

  it("throws ApiError carrying the structured body + status on error", async () => {
    const response = new Response(null, {
      status: 404,
      headers: { "X-RateLimit-Limit": "60", "X-RateLimit-Remaining": "59" },
    });
    const result = {
      error: {
        error: "persona_not_found",
        detail: "persona not found",
        context: { id: "p9" },
      },
      response,
    };
    await expect(unwrap(result)).rejects.toBeInstanceOf(ApiError);
    const err = await unwrap<never>(result).catch(
      (e: unknown) => e as ApiError,
    );
    expect(err.status).toBe(404);
    expect(err.code).toBe("persona_not_found");
    expect(err.context).toEqual({ id: "p9" });
    expect(err.rateLimit.limit).toBe(60);
    expect(err.isRateLimited).toBe(false);
  });

  it("flags 429 as rate-limited", async () => {
    const result = {
      error: { error: "rate_limit_exceeded" },
      response: new Response(null, {
        status: 429,
        headers: { "Retry-After": "30" },
      }),
    };
    const err = await unwrap<never>(result).catch(
      (e: unknown) => e as ApiError,
    );
    expect(err.isRateLimited).toBe(true);
    expect(err.rateLimit.retryAfter).toBe(30);
  });
});

describe("an erased account (R9-284 T9, review L6)", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function answer(status: number, body: object) {
    return new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    });
  }

  it("is a 410 with the account_erased code, and nothing else", async () => {
    expect(
      await isAccountErased(answer(410, { error: "account_erased" })),
    ).toBe(true);
    expect(await isAccountErased(answer(410, { error: "gone" }))).toBe(false);
    expect(
      await isAccountErased(answer(401, { error: "account_erased" })),
    ).toBe(false);
  });

  it("sends the session away when the API says so", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => answer(410, { error: "account_erased", detail: "x" })),
    );
    const onErased = vi.fn();
    const client = createApiClient(async () => "token", onErased);

    await client.GET("/v1/me/credits");

    expect(onErased).toHaveBeenCalledOnce();
  });

  it("leaves every other answer alone", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => answer(404, { error: "not_found" })),
    );
    const onErased = vi.fn();
    const client = createApiClient(async () => "token", onErased);

    await client.GET("/v1/me/credits");

    expect(onErased).not.toHaveBeenCalled();
  });
});
