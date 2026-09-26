/**
 * BUG 1 — `serverAuthToken` makes a gone session resilient instead of a crash.
 *
 * During a logout race the request cookie still names a `sess_…` Clerk has
 * already destroyed, so `getToken()` throws a 404 `ClerkAPIResponseError`
 * ("Session not found", code `resource_not_found`). `serverApi` must NOT let
 * that escape (it would 500 the server render); `serverAuthToken` translates it
 * to `{ signedOut: true }` so `serverApi` can `redirect("/sign-in")`. A real
 * signed-in request still returns its token; a non-session Clerk failure is
 * re-raised (a real bug, not a logout).
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

// server.cloud imports "server-only" (a no-op guard outside RSC).
vi.mock("server-only", () => ({}));

const auth = vi.fn();
vi.mock("@clerk/nextjs/server", () => ({
  auth,
  currentUser: vi.fn(),
}));

// Narrow on the structural shape the real `isClerkAPIResponseError` checks
// (an `errors: ClerkAPIError[]` array + numeric `status`).
vi.mock("@clerk/nextjs/errors", () => ({
  isClerkAPIResponseError: (e: unknown): boolean =>
    typeof e === "object" &&
    e !== null &&
    Array.isArray((e as { errors?: unknown }).errors),
}));

/** Build a Clerk-shaped API error (what `getToken()` throws on a gone session). */
function clerkApiError(status: number, code: string) {
  return Object.assign(new Error("Session not found"), {
    status,
    errors: [{ code, message: "Session not found" }],
  });
}

describe("server EDITION (cloud)", () => {
  it("names the cloud edition", async () => {
    const { EDITION } = await import("./server.cloud");
    expect(EDITION).toBe("cloud");
  });
});

describe("serverAuthToken (cloud) — BUG 1 logout-race resilience", () => {
  beforeEach(() => {
    auth.mockReset();
  });

  it("returns the token on a valid signed-in session", async () => {
    auth.mockResolvedValue({ getToken: vi.fn().mockResolvedValue("jwt-123") });
    const { serverAuthToken } = await import("./server.cloud");
    await expect(serverAuthToken("api")).resolves.toEqual({
      signedOut: false,
      token: "jwt-123",
    });
  });

  it("reports signed-out (not a throw) when getToken raises the 404 'Session not found'", async () => {
    auth.mockResolvedValue({
      getToken: vi
        .fn()
        .mockRejectedValue(clerkApiError(404, "resource_not_found")),
    });
    const { serverAuthToken } = await import("./server.cloud");
    await expect(serverAuthToken("api")).resolves.toEqual({
      signedOut: true,
      token: null,
    });
  });

  it("reports signed-out for a 401 / session_* code", async () => {
    const { serverAuthToken } = await import("./server.cloud");
    auth.mockResolvedValue({
      getToken: vi.fn().mockRejectedValue(clerkApiError(401, "whatever")),
    });
    await expect(serverAuthToken(undefined)).resolves.toEqual({
      signedOut: true,
      token: null,
    });
    auth.mockResolvedValue({
      getToken: vi
        .fn()
        .mockRejectedValue(clerkApiError(400, "session_expired")),
    });
    await expect(serverAuthToken(undefined)).resolves.toEqual({
      signedOut: true,
      token: null,
    });
  });

  it("treats a null token as signed-out", async () => {
    auth.mockResolvedValue({ getToken: vi.fn().mockResolvedValue(null) });
    const { serverAuthToken } = await import("./server.cloud");
    await expect(serverAuthToken("api")).resolves.toEqual({
      signedOut: true,
      token: null,
    });
  });

  it("re-raises a non-session Clerk failure (a real bug, not a logout)", async () => {
    auth.mockResolvedValue({
      getToken: vi
        .fn()
        .mockRejectedValue(clerkApiError(500, "internal_clerk_error")),
    });
    const { serverAuthToken } = await import("./server.cloud");
    await expect(serverAuthToken("api")).rejects.toThrow();
  });

  it("re-raises a plain (non-Clerk) error", async () => {
    auth.mockRejectedValue(new Error("boom"));
    const { serverAuthToken } = await import("./server.cloud");
    await expect(serverAuthToken("api")).rejects.toThrow("boom");
  });
});
