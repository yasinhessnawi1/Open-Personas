"use client";

import { useEffect, useState } from "react";
import { useAuth } from "@/auth";
import {
  authedImageCacheKey,
  loadCachedImage,
  peekCachedImage,
} from "@/lib/authed-image-cache";

const API = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";
const TEMPLATE = process.env.NEXT_PUBLIC_CLERK_JWT_TEMPLATE;

/**
 * F3 (T10) — fetch an authed image from Spec 13's serve endpoint
 * (D-F3-X-image-serve-auth).
 *
 * `<img src>` cannot render `GET /v1/personas/:id/uploads/:ref` directly:
 * the endpoint requires `Authorization: Bearer <jwt>` (auth/deps.py:62-64)
 * and browsers never send Authorization headers on image GETs. So this
 * hook does the fetch with the Bearer token, wraps the response blob in
 * `URL.createObjectURL`, and hands back the object URL the `<img>` tag
 * can render.
 *
 * **Hook discipline (load-bearing per the decision):**
 *   (a) The object URL and the in-flight fetch belong to the shared
 *       per-session cache (`@/lib/authed-image-cache`), not to one mount.
 *       A ref that is already cached is returned during render, so the
 *       image paints on the first frame with no loading state, and N
 *       mounts asking for the same ref at once share one fetch. Revoking
 *       happens on cache eviction, never on unmount, because the URL is
 *       shared. That also means the fetch is no longer aborted on unmount:
 *       letting it finish is what fills the cache for the next mount.
 *   (b) 404 gives a null `src` and a null `error` so the `<AuthedImage>`
 *       consumer renders a "image unavailable" placeholder, and nothing is
 *       cached, so an avatar that is still being generated is picked up by
 *       a later mount.
 *       401 is re-thrown so the Clerk session refresh runs (callers can
 *       branch on this if needed).
 *       5xx sets `error` so the consumer can render a retry affordance.
 */
export interface AuthedImageBlobUrlState {
  src: string | null;
  loading: boolean;
  /** Set for 5xx (and other unexpected failures); not for 404. */
  error: Error | null;
}

/** Cached refs start resolved; everything else starts in the loading state. */
function initialState(key: string): AuthedImageBlobUrlState {
  const cached = peekCachedImage(key);
  return cached === null
    ? { src: null, loading: true, error: null }
    : { src: cached, loading: false, error: null };
}

export function useAuthedImageBlobUrl(
  personaId: string,
  workspacePath: string,
): AuthedImageBlobUrlState {
  const { getToken } = useAuth();
  const key = authedImageCacheKey(personaId, workspacePath);
  // Keyed state: when the ref changes mid-mount, the state for the OLD ref is
  // dropped during render (React's documented adjust-state-on-prop-change
  // pattern) so a cached new ref paints immediately and an uncached one never
  // shows the previous persona's portrait for a frame.
  const [held, setHeld] = useState(() => ({ key, state: initialState(key) }));
  if (held.key !== key) setHeld({ key, state: initialState(key) });
  const state = held.key === key ? held.state : initialState(key);

  useEffect(() => {
    let cancelled = false;

    loadCachedImage(key, async () => {
      const token = await getToken(
        TEMPLATE ? { template: TEMPLATE } : undefined,
      );
      const res = await fetch(
        `${API}/v1/personas/${encodeURIComponent(personaId)}/uploads/${workspacePath}`,
        { headers: token ? { Authorization: `Bearer ${token}` } : {} },
      );
      if (res.status === 404) {
        // Existence-disclosure-safe per D-08-1; consumer renders placeholder.
        return null;
      }
      if (!res.ok) throw new Error(`image fetch ${res.status}`);
      return await res.blob();
    })
      .then((src) => {
        if (cancelled) return;
        setHeld({ key, state: { src, loading: false, error: null } });
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setHeld({
          key,
          state: {
            src: null,
            loading: false,
            error: e instanceof Error ? e : new Error(String(e)),
          },
        });
      });

    return () => {
      cancelled = true;
    };
  }, [key, personaId, workspacePath, getToken]);

  return state;
}
