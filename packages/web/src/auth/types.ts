/**
 * The stable auth surface both editions implement (Spec 33, D-33-X-auth-facade-surface).
 *
 * Every Clerk touch is isolated behind `@/auth` (client), `@/auth/server`,
 * `@/auth/provider`, and `@/auth/middleware`. The `cloud` variant is Clerk; the
 * `community` variant is a no-auth stub (fixed local owner). The edition is
 * selected at BUILD time via `turbopack.resolveAlias` keyed on `PERSONA_EDITION`
 * (next.config.ts), so a community build never pulls `@clerk/*` into the bundle.
 *
 * Both variants conform to the types here so call sites are edition-agnostic.
 */
import type { ComponentType, ReactNode } from "react";

/**
 * Which edition this build is. Each `@/auth/server` variant exports it as a constant,
 * so it is exactly the edition the build selected, with no runtime environment read
 * that could differ from the build's.
 */
export type Edition = "cloud" | "community";

/** Mint a bearer token for the API (Clerk JWT-template token in cloud; null in community). */
export type GetToken = (options?: {
  template?: string;
}) => Promise<string | null>;

/** The client-side auth state (the subset every call site uses: just the token getter). */
export interface AuthState {
  getToken: GetToken;
}

/** The server-side auth result (owner id + token getter). */
export interface ServerAuth {
  userId: string | null;
  getToken: GetToken;
}

/**
 * Outcome of resolving the server-side API bearer token (`serverAuthToken`).
 *
 * `signedOut` is `true` when the request's session is gone (logout race / stale
 * cookie / no token) so the caller redirects to `/sign-in` instead of crashing.
 * In community there is no sign-in wall, so it is always `false`.
 */
export interface ServerTokenResult {
  signedOut: boolean;
  token: string | null;
}

/** The current user's profile shape the settings page reads. */
export interface CurrentUser {
  primaryEmailAddress?: { emailAddress?: string | null } | null;
  firstName?: string | null;
  lastName?: string | null;
}

/** Wraps the app tree (ClerkProvider in cloud; passthrough in community). */
export type AuthProviderComponent = ComponentType<{ children: ReactNode }>;

/**
 * The client-side account surface the custom sidebar account menu reads
 * (Spec 35 D-35-16). Cloud feeds it from Clerk (`useUser` + `useClerk`);
 * community returns a degraded shape (fixed local owner — no name/avatar, no
 * sign-out / manage-account actions). The presentational `<AccountMenu>` is
 * edition-agnostic: it imports this hook from `@/auth`, never `@clerk/*`, so it
 * stays in the community-reachable graph and `check:clerk-free` stays green.
 */
export interface Account {
  /** Display name (full name in cloud; empty in community → menu falls back to a label). */
  name: string;
  /** Primary email if known (cloud); null in community. */
  email: string | null;
  /** Avatar image URL if any (Clerk `imageUrl` in cloud); null otherwise. */
  imageUrl: string | null;
  /** Whether account actions (sign-out / manage) are available — cloud only. */
  available: boolean;
  /** Sign the user out (cloud); undefined in community. */
  signOut?: () => void;
  /** Open the account-management UI (cloud); undefined in community. */
  manageAccount?: () => void;
}

/** The client account hook both editions implement behind `@/auth`. */
export type UseAccount = () => Account;
