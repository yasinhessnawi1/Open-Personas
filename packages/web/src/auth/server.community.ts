/**
 * `@/auth/server` — community (no-auth) server surface (Spec 33).
 *
 * Returns the fixed local owner. `auth().userId` is always set (the signed-in
 * branch is always taken — there is no sign-in wall), and `getToken` returns
 * null (the community API verifies no token).
 */
import "server-only";

import type {
  CurrentUser,
  Edition,
  ServerAuth,
  ServerTokenResult,
} from "./types";

/** This build is the community edition: unmetered, so no balance is ever money. */
export const EDITION: Edition = "community";

const LOCAL_OWNER_ID = "local-owner";
const LOCAL_OWNER_EMAIL = "local@localhost";

export async function auth(): Promise<ServerAuth> {
  return { userId: LOCAL_OWNER_ID, getToken: async () => null };
}

/**
 * Resolve the server-side API token. Community has no sign-in wall and the API
 * verifies no token, so this never reports `signedOut` and never throws — the
 * token is always `null` (matching the community `auth().getToken`).
 */
export async function serverAuthToken(
  _template: string | undefined,
): Promise<ServerTokenResult> {
  return { signedOut: false, token: null };
}

export async function currentUser(): Promise<CurrentUser> {
  return {
    primaryEmailAddress: { emailAddress: LOCAL_OWNER_EMAIL },
    firstName: "Local",
    lastName: "Owner",
  };
}
