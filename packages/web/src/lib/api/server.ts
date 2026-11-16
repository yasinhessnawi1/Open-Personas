import "server-only";

import { redirect } from "next/navigation";
import { serverAuthToken } from "@/auth/server";
import { ACCOUNT_DELETED_PATH, createApiClient } from "./client";

const TEMPLATE = process.env.NEXT_PUBLIC_CLERK_JWT_TEMPLATE;

/**
 * A typed API client for use in Server Components / Server Actions. Injects the
 * caller's Clerk JWT-template token (with the `aud` the API verifies) as
 * `Authorization: Bearer`. (D-09-1, D-09-2.)
 *
 * The token is resolved EAGERLY here (not lazily per-request) so a gone session
 * is handled at this boundary: during a logout race the request cookie still
 * names a session Clerk has already destroyed, and `getToken()` throws a 404
 * `ClerkAPIResponseError` ("Session not found"). Rather than let that escape and
 * crash the server render, `serverAuthToken` reports `signedOut`, and we
 * `redirect("/sign-in")` — the Next-idiomatic landing for an unauthenticated
 * server render. (`redirect` throws `NEXT_REDIRECT`; it is intentionally called
 * outside any try/catch.) In community, `signedOut` is never set and the token
 * is always `null`, so the normal no-auth path is unchanged.
 */
export async function serverApi() {
  const { signedOut, token } = await serverAuthToken(TEMPLATE);
  if (signedOut) redirect("/sign-in");
  // R9-284 T9 (review L6): an erased account's request redirects to the goodbye page.
  return createApiClient(
    async () => token,
    () => redirect(ACCOUNT_DELETED_PATH),
  );
}
