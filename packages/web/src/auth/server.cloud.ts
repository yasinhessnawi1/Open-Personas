/**
 * `@/auth/server` — cloud (Clerk) server surface (Spec 33).
 *
 * The server-side auth functions used by Server Components / Actions: `auth()`
 * (owner id + token getter) and `currentUser()` (profile). Re-exported straight
 * from Clerk — cloud behavior is unchanged.
 */
import "server-only";

import { isClerkAPIResponseError } from "@clerk/nextjs/errors";
import { auth as clerkAuth } from "@clerk/nextjs/server";
import type { Edition, ServerTokenResult } from "./types";

export { auth, currentUser } from "@clerk/nextjs/server";

/** This build is the cloud edition: metered, so the balance is money. */
export const EDITION: Edition = "cloud";

/**
 * Clerk API error codes that mean the session this request's cookie references
 * is gone (signed out / destroyed / not found). During a logout race the cookie
 * still names a `sess_…` that Clerk has already destroyed, so `getToken()`
 * throws a 404 `resource_not_found` ("Session not found") rather than returning
 * a token. We treat any of these — and a 401 — as "signed out", not a crash.
 */
const SIGNED_OUT_CODES = new Set<string>([
  "resource_not_found",
  "session_not_found",
  "authentication_invalid",
  "session_token_and_uat_claim_check_failed",
]);

/** True when a thrown error means the session is gone (logout race / stale cookie). */
function isSignedOutError(error: unknown): boolean {
  if (!isClerkAPIResponseError(error)) return false;
  if (error.status === 401) return true;
  return error.errors.some(
    (e) =>
      (e.code !== undefined && SIGNED_OUT_CODES.has(e.code)) ||
      e.code?.startsWith("session_") === true,
  );
}

/**
 * Resolve the caller's API bearer token, never throwing on a gone session.
 *
 * Returns `{ signedOut: true }` when the session referenced by the request
 * cookie has been destroyed (the logout race) or is otherwise invalid, so the
 * caller (`serverApi`) can land the user on `/sign-in` instead of letting an
 * unhandled `ClerkAPIResponseError` ("Session not found", 404) crash the server
 * render. A genuinely-signed-in request still returns its token. Non-session
 * Clerk failures are re-raised (they are real bugs, not a logout).
 */
export async function serverAuthToken(
  template: string | undefined,
): Promise<ServerTokenResult> {
  let getToken: (options?: { template?: string }) => Promise<string | null>;
  try {
    ({ getToken } = await clerkAuth());
  } catch (error) {
    if (isSignedOutError(error)) return { signedOut: true, token: null };
    throw error;
  }
  try {
    const token = await getToken(template ? { template } : undefined);
    if (!token) return { signedOut: true, token: null };
    return { signedOut: false, token };
  } catch (error) {
    if (isSignedOutError(error)) return { signedOut: true, token: null };
    throw error;
  }
}
