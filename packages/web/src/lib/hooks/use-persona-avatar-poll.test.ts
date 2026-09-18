import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { usePersonaAvatarPoll } from "./use-persona-avatar-poll";

// `@/auth` resolves to the cloud barrel (re-exports useAuth from @clerk/nextjs);
// mock it so the hook gets a stub token getter with no real Clerk wiring.
vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({
    getToken: () => Promise.resolve("jwt-token"),
  }),
}));

const INTERVAL_MS = 2500;
const MAX_MS = 40_000;

function personaResponse(
  avatarUrl: string | null,
  avatarStatus: "pending" | "failed" | null = null,
): Response {
  const res = new Response(null, { status: 200 });
  Object.defineProperty(res, "json", {
    value: () =>
      Promise.resolve({ avatar_url: avatarUrl, avatar_status: avatarStatus }),
  });
  return res;
}

describe("usePersonaAvatarPoll — async-persona-create bounded poll", () => {
  let fetchCalls: Array<{ url: string; init?: RequestInit }>;

  beforeEach(() => {
    vi.useFakeTimers();
    fetchCalls = [];
  });
  afterEach(() => {
    vi.runOnlyPendingTimers();
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  function mockFetch(...responses: (string | null)[]): void {
    let i = 0;
    globalThis.fetch = vi.fn(async (url, init) => {
      fetchCalls.push({
        url: typeof url === "string" ? url : url.toString(),
        init,
      });
      const value = i < responses.length ? responses[i] : null;
      i += 1;
      return personaResponse(value);
    }) as unknown as typeof fetch;
  }

  it("does NOT poll when the server already provided an avatar_url", async () => {
    mockFetch("uploads/x.png");
    const { result } = renderHook(() =>
      usePersonaAvatarPoll("p", "uploads/seed.png"),
    );
    // Advance well past the cap — still no fetch (nothing to wait for).
    await act(async () => {
      await vi.advanceTimersByTimeAsync(MAX_MS + INTERVAL_MS);
    });
    expect(fetchCalls).toHaveLength(0);
    expect(result.current).toBe("uploads/seed.png");
  });

  it("polls GET /v1/personas/{id} and swaps in the avatar once it appears", async () => {
    // First tick: still null. Second tick: avatar ready.
    mockFetch(null, "uploads/generated.png");
    const { result } = renderHook(() => usePersonaAvatarPoll("p_async", null));

    expect(result.current).toBeNull();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(INTERVAL_MS); // tick 1 → null
    });
    expect(result.current).toBeNull();
    expect(fetchCalls[0].url).toContain("/v1/personas/p_async");
    expect(
      (fetchCalls[0].init?.headers as Record<string, string>).Authorization,
    ).toBe("Bearer jwt-token");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(INTERVAL_MS); // tick 2 → avatar
    });
    expect(result.current).toBe("uploads/generated.png");
  });

  it("STOPS polling once the avatar arrives (no further fetches)", async () => {
    mockFetch(null, "uploads/done.png");
    renderHook(() => usePersonaAvatarPoll("p", null));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(INTERVAL_MS * 2); // resolves on tick 2
    });
    const callsAfterResolve = fetchCalls.length;

    // Advance far beyond — no additional polls fire after success.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(MAX_MS);
    });
    expect(fetchCalls.length).toBe(callsAfterResolve);
  });

  it("is strictly bounded — stops at the cap if the avatar never arrives", async () => {
    mockFetch(); // always null
    renderHook(() => usePersonaAvatarPoll("p", null));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(MAX_MS); // exactly the cap
    });
    const atCap = fetchCalls.length;
    expect(atCap).toBeGreaterThan(0);
    expect(atCap).toBeLessThanOrEqual(MAX_MS / INTERVAL_MS);

    // Well past the cap: no more polling (the bound stopped the interval).
    await act(async () => {
      await vi.advanceTimersByTimeAsync(MAX_MS * 2);
    });
    expect(fetchCalls.length).toBe(atCap);
  });

  it("stops early when the server says the durable generation failed", async () => {
    // Tick 1: still pending. Tick 2: the job dead-lettered. Nothing will arrive.
    let i = 0;
    const statuses: Array<"pending" | "failed"> = ["pending", "failed"];
    globalThis.fetch = vi.fn(async (url, init) => {
      fetchCalls.push({
        url: typeof url === "string" ? url : url.toString(),
        init,
      });
      const status = statuses[Math.min(i, statuses.length - 1)];
      i += 1;
      return personaResponse(null, status);
    }) as unknown as typeof fetch;

    const { result } = renderHook(() => usePersonaAvatarPoll("p", null));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(INTERVAL_MS * 2);
    });
    expect(result.current).toBeNull();
    const callsAtFailure = fetchCalls.length;
    expect(callsAtFailure).toBe(2);

    // Well before the cap: no further polls once the failure is known.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(MAX_MS);
    });
    expect(fetchCalls.length).toBe(callsAtFailure);
  });

  it("cancels in-flight fetch + clears the interval on unmount", async () => {
    let aborted = false;
    globalThis.fetch = vi.fn(
      (_url, init) =>
        new Promise((_resolve, reject) => {
          (init as RequestInit).signal?.addEventListener("abort", () => {
            aborted = true;
            reject(new Error("aborted"));
          });
        }),
    ) as unknown as typeof fetch;

    const { unmount } = renderHook(() => usePersonaAvatarPoll("p", null));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(INTERVAL_MS); // kick off one fetch
    });
    const callsBeforeUnmount = (globalThis.fetch as ReturnType<typeof vi.fn>)
      .mock.calls.length;

    unmount();
    expect(aborted).toBe(true);

    // No further ticks after unmount (interval cleared).
    await act(async () => {
      await vi.advanceTimersByTimeAsync(MAX_MS);
    });
    expect(
      (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.length,
    ).toBe(callsBeforeUnmount);
  });
});
