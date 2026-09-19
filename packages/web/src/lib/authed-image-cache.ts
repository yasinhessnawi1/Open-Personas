"use client";

/**
 * Per-session cache for images that live behind the Bearer-authed serve route
 * (`GET /v1/personas/:id/uploads/:ref`).
 *
 * Why this exists: a persona avatar cannot be a plain `<img src>` (the route
 * needs an Authorization header the browser never sends on image GETs), so the
 * web fetches the bytes and wraps them in an object URL. Before this module
 * every mount did its own fetch and revoked its own object URL on unmount, so
 * the same avatar was refetched on every navigation and in every list row, and
 * the portrait visibly popped in after the initials mark.
 *
 * The cache holds one object URL per (persona, ref) for the life of the page:
 *
 *   * `peekCachedImage` is a pure read used during render, so a ref that is
 *     already cached renders on the FIRST frame of the next mount with no
 *     loading state at all.
 *   * `loadCachedImage` deduplicates: N components asking for the same ref
 *     while a fetch is in flight share that one fetch and one object URL.
 *   * The cache is bounded by entry count AND by bytes (chat images and
 *     rasterised PDF pages go through the same route as avatars and are much
 *     larger). Evicting an entry revokes its object URL, which is the only
 *     place a revoke happens now.
 *
 * Consequence worth stating: unmounting no longer revokes and no longer aborts
 * the fetch. Both would be wrong here, because the object URL and the in-flight
 * request are shared with every other mount that wants the same ref, and the
 * whole point is that the bytes outlive the component that asked for them.
 *
 * Safety: entries are keyed by persona id plus workspace ref. A persona id is
 * only ever visible to its owner (cross-tenant reads 404 under RLS), so a
 * cached entry can only ever be re-read by the same owner in the same tab.
 * `clearAuthedImageCache` drops everything when the session ends.
 */

/** Maximum number of cached images before the least recently used is dropped. */
export const AUTHED_IMAGE_CACHE_MAX_ENTRIES = 200;

/** Maximum total bytes held as object URLs before the oldest are dropped. */
export const AUTHED_IMAGE_CACHE_MAX_BYTES = 48 * 1024 * 1024;

interface CacheEntry {
  url: string;
  bytes: number;
}

/** Insertion order is recency order: the first key is the least recently used. */
const entries = new Map<string, CacheEntry>();
const inFlight = new Map<string, Promise<string | null>>();
let totalBytes = 0;

/** The cache key for one image. Both avatar hooks must derive it the same way. */
export function authedImageCacheKey(
  personaId: string,
  workspaceRef: string,
): string {
  return `${personaId}::${workspaceRef}`;
}

/**
 * Read a cached object URL without touching recency. Safe to call during
 * render, which is what makes a cached avatar paint synchronously.
 */
export function peekCachedImage(key: string): string | null {
  return entries.get(key)?.url ?? null;
}

function touch(key: string): CacheEntry | undefined {
  const entry = entries.get(key);
  if (entry === undefined) return undefined;
  entries.delete(key);
  entries.set(key, entry);
  return entry;
}

function evictUntilWithinBounds(): void {
  while (
    entries.size > AUTHED_IMAGE_CACHE_MAX_ENTRIES ||
    totalBytes > AUTHED_IMAGE_CACHE_MAX_BYTES
  ) {
    const oldest = entries.keys().next();
    if (oldest.done) return;
    const entry = entries.get(oldest.value);
    entries.delete(oldest.value);
    if (entry !== undefined) {
      totalBytes -= entry.bytes;
      URL.revokeObjectURL(entry.url);
    }
  }
}

/**
 * Resolve the object URL for `key`, fetching at most once per key.
 *
 * `fetcher` returns the image bytes, or `null` when the image is genuinely
 * absent (a 404 on a still-generating avatar). A `null` result is NOT cached,
 * so an avatar that appears later is picked up by the next mount. A throwing
 * `fetcher` (a 5xx) is not cached either, and the rejection reaches every
 * caller that joined the same in-flight fetch.
 */
export function loadCachedImage(
  key: string,
  fetcher: () => Promise<Blob | null>,
): Promise<string | null> {
  const cached = touch(key);
  if (cached !== undefined) return Promise.resolve(cached.url);

  const pending = inFlight.get(key);
  if (pending !== undefined) return pending;

  const started = fetcher()
    .then((blob) => {
      if (blob === null) return null;
      // A second resolution for the same key can only happen after the
      // in-flight entry is cleared, so re-use the winner and revoke nothing.
      const existing = entries.get(key);
      if (existing !== undefined) return existing.url;
      const url = URL.createObjectURL(blob);
      entries.set(key, { url, bytes: blob.size });
      totalBytes += blob.size;
      evictUntilWithinBounds();
      return url;
    })
    .finally(() => {
      inFlight.delete(key);
    });

  inFlight.set(key, started);
  return started;
}

/**
 * Drop every cached image and revoke its object URL.
 *
 * Called when the signed-in session ends, so a browser left on a shared
 * machine holds no persona portraits in memory after sign out.
 */
export function clearAuthedImageCache(): void {
  for (const entry of entries.values()) URL.revokeObjectURL(entry.url);
  entries.clear();
  inFlight.clear();
  totalBytes = 0;
}
