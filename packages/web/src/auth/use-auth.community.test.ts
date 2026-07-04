/**
 * R4-C1-6 regression pin: the community auth stubs must be REFERENTIALLY
 * STABLE across renders, matching the guarantee Clerk's real hooks provide.
 *
 * Why this is load-bearing: consumers put `getToken` into `useCallback` /
 * `useEffect` dependency chains (use-conversation-artifacts, conversation
 * uploads, and others). A fresh object per call makes every such effect
 * re-fire on every render, and any effect that also sets state becomes an
 * unbounded render→fetch loop — the community chat Files viewer produced
 * ~120k requests against /artifacts + /uploads before rate-limiting (the
 * 5ae0e10 voice-selector OOM class). Identity equality here IS the contract.
 */

import { describe, expect, it } from "vitest";
import { useAccount } from "./account.community";
import { useAuth } from "./use-auth.community";

describe("community auth stubs — referential stability (R4-C1-6)", () => {
  it("useAuth returns the SAME object and getToken identity every call", () => {
    const a = useAuth();
    const b = useAuth();
    expect(a).toBe(b);
    expect(a.getToken).toBe(b.getToken);
  });

  it("useAuth getToken resolves null (community sends no bearer)", async () => {
    await expect(useAuth().getToken()).resolves.toBeNull();
  });

  it("useAccount returns the SAME object every call", () => {
    expect(useAccount()).toBe(useAccount());
    expect(useAccount().available).toBe(false);
  });
});
