/**
 * `useAccount` (cloud), the sign-out action drops the session's cached
 * persona portraits before it drops the session.
 */
import { renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  authedImageCacheKey,
  clearAuthedImageCache,
  loadCachedImage,
  peekCachedImage,
} from "@/lib/authed-image-cache";

const clerkSignOut = vi.fn();
vi.mock("@clerk/nextjs", () => ({
  useUser: () => ({ user: { fullName: "Ada", username: "ada" } }),
  useClerk: () => ({ signOut: clerkSignOut, openUserProfile: vi.fn() }),
}));

import { useAccount } from "./account.cloud";

describe("useAccount (cloud) sign out", () => {
  beforeEach(() => {
    clerkSignOut.mockReset();
    globalThis.URL.createObjectURL = vi.fn(() => "blob:portrait");
    globalThis.URL.revokeObjectURL = vi.fn();
    clearAuthedImageCache();
  });

  it("revokes the cached portraits and signs out", async () => {
    const key = authedImageCacheKey("p1", "uploads/abc.png");
    await loadCachedImage(key, async () => ({ size: 10 }) as Blob);
    expect(peekCachedImage(key)).toBe("blob:portrait");

    const { result } = renderHook(() => useAccount());
    result.current.signOut?.();

    expect(peekCachedImage(key)).toBeNull();
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:portrait");
    expect(clerkSignOut).toHaveBeenCalled();
  });
});
