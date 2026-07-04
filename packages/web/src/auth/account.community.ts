"use client";

/**
 * `useAccount` — community (no-auth) account surface (Spec 35 D-35-16).
 *
 * Single local owner: no Clerk identity, no sign-out / manage-account actions.
 * The custom `<AccountMenu>` renders the same design degraded to settings +
 * appearance only. Selected for `PERSONA_EDITION=community` builds.
 */

import type { Account } from "./types";

// Same referential-stability contract as `useAuth` (R4-C1-6): one frozen
// instance so effect/callback deps on the account surface never churn.
const _STABLE_ACCOUNT: Account = Object.freeze({
  name: "",
  email: null,
  imageUrl: null,
  available: false,
});

export function useAccount(): Account {
  return _STABLE_ACCOUNT;
}
