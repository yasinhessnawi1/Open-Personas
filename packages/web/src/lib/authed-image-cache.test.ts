import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  AUTHED_IMAGE_CACHE_MAX_BYTES,
  AUTHED_IMAGE_CACHE_MAX_ENTRIES,
  authedImageCacheKey,
  clearAuthedImageCache,
  loadCachedImage,
  peekCachedImage,
} from "./authed-image-cache";

/** A blob stand-in with a controllable size (jsdom Blob sizing is fine, but
 *  the byte-bound test needs megabyte-scale blobs without the allocation). */
function blobOf(bytes: number): Blob {
  return { size: bytes, type: "image/png" } as Blob;
}

describe("authed image cache", () => {
  let minted: number;

  beforeEach(() => {
    minted = 0;
    globalThis.URL.createObjectURL = vi.fn(() => {
      minted += 1;
      return `blob:cached-${minted}`;
    });
    globalThis.URL.revokeObjectURL = vi.fn();
    clearAuthedImageCache();
  });

  afterEach(() => {
    clearAuthedImageCache();
    vi.restoreAllMocks();
  });

  it("keys by persona and ref so two personas never share an entry", () => {
    expect(authedImageCacheKey("p1", "uploads/a.png")).not.toBe(
      authedImageCacheKey("p2", "uploads/a.png"),
    );
  });

  it("deduplicates: two callers for one ref share a single fetch", async () => {
    const fetcher = vi.fn(async () => blobOf(10));
    const key = authedImageCacheKey("p1", "uploads/a.png");

    const [first, second] = await Promise.all([
      loadCachedImage(key, fetcher),
      loadCachedImage(key, fetcher),
    ]);

    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(first).toBe(second);
    expect(URL.createObjectURL).toHaveBeenCalledTimes(1);
  });

  it("serves a cached ref without fetching again", async () => {
    const key = authedImageCacheKey("p1", "uploads/a.png");
    const first = vi.fn(async () => blobOf(10));
    const url = await loadCachedImage(key, first);

    const second = vi.fn(async () => blobOf(10));
    expect(peekCachedImage(key)).toBe(url);
    expect(await loadCachedImage(key, second)).toBe(url);
    expect(second).not.toHaveBeenCalled();
  });

  it("peeks null for a ref that was never loaded", () => {
    expect(peekCachedImage(authedImageCacheKey("p1", "uploads/nope.png"))).toBe(
      null,
    );
  });

  it("evicts the least recently used entry and revokes its object URL", async () => {
    const oldest = authedImageCacheKey("p1", "uploads/oldest.png");
    const oldestUrl = await loadCachedImage(oldest, async () => blobOf(1));
    const second = authedImageCacheKey("p1", "uploads/second.png");
    await loadCachedImage(second, async () => blobOf(1));

    // Re-reading the oldest entry makes the SECOND one the eviction victim.
    await loadCachedImage(oldest, async () => blobOf(1));

    for (let i = 0; i < AUTHED_IMAGE_CACHE_MAX_ENTRIES; i += 1) {
      await loadCachedImage(
        authedImageCacheKey("p1", `uploads/fill-${i}.png`),
        async () => blobOf(1),
      );
    }

    expect(peekCachedImage(second)).toBeNull();
    expect(peekCachedImage(oldest)).toBeNull();
    expect(URL.revokeObjectURL).toHaveBeenCalledWith(oldestUrl);
  });

  it("evicts on the byte bound, not only the entry count", async () => {
    const first = authedImageCacheKey("p1", "uploads/big-1.png");
    const firstUrl = await loadCachedImage(first, async () =>
      blobOf(AUTHED_IMAGE_CACHE_MAX_BYTES / 2),
    );
    const second = authedImageCacheKey("p1", "uploads/big-2.png");
    const secondUrl = await loadCachedImage(second, async () =>
      blobOf(AUTHED_IMAGE_CACHE_MAX_BYTES),
    );

    expect(peekCachedImage(first)).toBeNull();
    expect(peekCachedImage(second)).toBe(secondUrl);
    expect(URL.revokeObjectURL).toHaveBeenCalledWith(firstUrl);
  });

  it("does not cache a missing image, so a later upload is picked up", async () => {
    const key = authedImageCacheKey("p1", "uploads/pending.png");
    expect(await loadCachedImage(key, async () => null)).toBeNull();
    expect(peekCachedImage(key)).toBeNull();

    const url = await loadCachedImage(key, async () => blobOf(10));
    expect(url).toMatch(/^blob:cached-/);
  });

  it("does not cache a failure, so the next mount retries", async () => {
    const key = authedImageCacheKey("p1", "uploads/flaky.png");
    await expect(
      loadCachedImage(key, async () => {
        throw new Error("image fetch 503");
      }),
    ).rejects.toThrow("image fetch 503");

    const retry = vi.fn(async () => blobOf(10));
    expect(await loadCachedImage(key, retry)).toMatch(/^blob:cached-/);
    expect(retry).toHaveBeenCalledTimes(1);
  });

  it("clears every entry and revokes on sign out", async () => {
    const key = authedImageCacheKey("p1", "uploads/a.png");
    const url = await loadCachedImage(key, async () => blobOf(10));
    clearAuthedImageCache();
    expect(peekCachedImage(key)).toBeNull();
    expect(URL.revokeObjectURL).toHaveBeenCalledWith(url);
  });
});
