"use client";

/**
 * Spec V6 — resolve a persona `avatar_url` to a loadable `<img>` src.
 *
 * Spec 29 auto-generates avatars and stores `avatar_url` as a BARE Bearer-auth
 * workspace ref (`uploads/<blake2b>.png`) served from
 * `GET /v1/personas/:id/uploads/:ref` — a plain `<img src>` cannot load it
 * (the route needs an Authorization header browsers never send on image GETs).
 * Spec 29 shipped no frontend, so generated avatars don't render anywhere yet;
 * this resolves them for the call orb (D-V6-3 — the avatar is the orb's core).
 *
 *   - direct URLs (http/https/blob/data — e.g. a user-supplied avatar) pass
 *     through unchanged, no fetch;
 *   - a bare workspace ref goes through the shared per-session image cache
 *     (`@/lib/authed-image-cache`), the same one `useAuthedImageBlobUrl` uses
 *     and under the same key, so a persona whose portrait was already shown in
 *     the roster or the chat header opens the call orb with no fetch at all;
 *   - null/empty → null, and NO fetch happens (the no-avatar case stays inert).
 *
 * The cache owns the object URL, so this hook never revokes: the orb's portrait
 * is the same object URL the rest of the app is showing.
 */

import { useEffect, useState } from "react";
import { useAuth } from "@/auth";
import {
  authedImageCacheKey,
  loadCachedImage,
  peekCachedImage,
} from "@/lib/authed-image-cache";

const API = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";
const TEMPLATE = process.env.NEXT_PUBLIC_CLERK_JWT_TEMPLATE;
const DIRECT_URL = /^(https?:|blob:|data:)/;
/** The trailing `uploads/<file>` workspace ref, however the value is wrapped. */
const UPLOADS_REF = /(uploads\/[^/]+)$/;

/** Whether an avatar_url is a directly-loadable URL (vs a workspace ref). */
export function isDirectAvatarUrl(avatarUrl: string): boolean {
  return DIRECT_URL.test(avatarUrl);
}

/**
 * Normalise an avatar value to the bare workspace ref the serve route expects
 * (`uploads/<hash>.<ext>`). Tolerates either the bare ref OR a full route path
 * (`/v1/personas/:id/uploads/uploads/<hash>.png`) so the route prefix is never
 * doubled — the column has been observed to hold both shapes.
 */
export function avatarWorkspaceRef(avatarUrl: string): string {
  const match = avatarUrl.match(UPLOADS_REF);
  return match ? match[1] : avatarUrl.replace(/^\/+/, "");
}

/** The resolved src a persona avatar starts with, before any fetch. */
function initialSrc(
  personaId: string,
  avatarUrl: string | null | undefined,
): string | null {
  if (!avatarUrl) return null;
  if (isDirectAvatarUrl(avatarUrl)) return avatarUrl;
  return peekCachedImage(
    authedImageCacheKey(personaId, avatarWorkspaceRef(avatarUrl)),
  );
}

export function usePersonaAvatarSrc(
  personaId: string,
  avatarUrl: string | null | undefined,
): string | null {
  const { getToken } = useAuth();
  const [src, setSrc] = useState<string | null>(() =>
    initialSrc(personaId, avatarUrl),
  );

  useEffect(() => {
    if (!avatarUrl) {
      setSrc(null);
      return;
    }
    if (isDirectAvatarUrl(avatarUrl)) {
      setSrc(avatarUrl);
      return;
    }

    let cancelled = false;
    const ref = avatarWorkspaceRef(avatarUrl);

    loadCachedImage(authedImageCacheKey(personaId, ref), async () => {
      const token = await getToken(
        TEMPLATE ? { template: TEMPLATE } : undefined,
      );
      const res = await fetch(
        `${API}/v1/personas/${encodeURIComponent(personaId)}/uploads/${ref}`,
        { headers: token ? { Authorization: `Bearer ${token}` } : {} },
      );
      if (!res.ok) return null;
      return await res.blob();
    })
      .then((url) => {
        if (!cancelled && url !== null) setSrc(url);
      })
      .catch(() => {
        // Avatar is decorative on the call surface — fall back to the orb's
        // identity fill + initials (D-V6-1 works avatar-or-not). Never throw.
      });

    return () => {
      cancelled = true;
    };
  }, [personaId, avatarUrl, getToken]);

  return src;
}
