/**
 * Spec 33 (Cluster C) — the community `@/auth` variants behave as a no-auth stub.
 *
 * These import the `.community` variant files directly (the build selects them
 * via turbopack.resolveAlias; here we assert their behavior).
 */
import { describe, expect, it, vi } from "vitest";

// server.community imports "server-only" (a no-op guard outside RSC).
vi.mock("server-only", () => ({}));

import { useAuth } from "./use-auth.community";

describe("community @/auth", () => {
  it("useAuth mints no token (community API is no-auth)", async () => {
    const { getToken } = useAuth();
    await expect(getToken()).resolves.toBeNull();
    await expect(getToken({ template: "anything" })).resolves.toBeNull();
  });

  it("server auth() returns the fixed local owner (signed-in branch always taken)", async () => {
    const { auth } = await import("./server.community");
    const { userId, getToken } = await auth();
    expect(userId).toBe("local-owner");
    await expect(getToken()).resolves.toBeNull();
  });

  it("server EDITION names the community edition", async () => {
    const { EDITION } = await import("./server.community");
    expect(EDITION).toBe("community");
  });

  it("server currentUser() returns the local owner profile shape settings reads", async () => {
    const { currentUser } = await import("./server.community");
    const user = await currentUser();
    expect(user.primaryEmailAddress?.emailAddress).toBe("local@localhost");
    expect(user.firstName).toBe("Local");
  });

  it("serverAuthToken never reports signed-out (no sign-in wall) and mints no token", async () => {
    const { serverAuthToken } = await import("./server.community");
    await expect(serverAuthToken(undefined)).resolves.toEqual({
      signedOut: false,
      token: null,
    });
    await expect(serverAuthToken("api")).resolves.toEqual({
      signedOut: false,
      token: null,
    });
  });

  it("middleware is a passthrough with an empty matcher (no route protection)", async () => {
    const mod = await import("./middleware.community");
    expect(mod.config.matcher).toEqual([]);
    expect(typeof mod.default).toBe("function");
  });
});
