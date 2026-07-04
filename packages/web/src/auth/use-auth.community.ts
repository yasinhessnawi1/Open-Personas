"use client";

import type { AuthState } from "./types";

/**
 * Community `useAuth`: no Clerk, no token. The API runs no-auth in community, so
 * `getToken` returns null and no `Authorization` header is sent.
 *
 * REFERENTIAL STABILITY IS THE CONTRACT (R4-C1-6, 2026-07-04): Clerk's real
 * `useAuth` returns a stable `getToken`, and consumers legitimately put it in
 * `useCallback`/`useEffect` dependency chains. A fresh object per render turned
 * every such consumer into an unbounded render→fetch loop in community builds
 * (the chat Files viewer alone produced ~120k requests — the same class as the
 * 5ae0e10 voice-selector OOM). The stub must honour the same stability Clerk
 * provides: one frozen module-level instance, forever.
 */
const _STABLE_AUTH: AuthState = Object.freeze({ getToken: async () => null });

export function useAuth(): AuthState {
  return _STABLE_AUTH;
}
