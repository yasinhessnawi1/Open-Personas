"use client";

/**
 * `useAccount` — cloud (Clerk) account surface (Spec 35 D-35-16).
 *
 * Feeds the custom `<AccountMenu>` from Clerk's client hooks. This is the ONLY
 * place the account name/avatar/actions touch `@clerk/*`; the menu component
 * itself is Clerk-free. Selected for `PERSONA_EDITION=cloud` builds via the
 * `@/auth` resolveAlias.
 */

import { useClerk, useUser } from "@clerk/nextjs";
import { clearAuthedImageCache } from "@/lib/authed-image-cache";
import type { Account } from "./types";

export function useAccount(): Account {
  const { user } = useUser();
  const clerk = useClerk();
  const email = user?.primaryEmailAddress?.emailAddress ?? null;
  return {
    // A REAL display name only (never the email as a fallback) — the account
    // menu shows the email once on its own line, so seeding `name` from the
    // email here made the button render the address twice (R4 T1). When the
    // user has no name set, `name` is empty and the menu shows the email once.
    name: user?.fullName || user?.username || "",
    email,
    imageUrl: user?.imageUrl ?? null,
    available: Boolean(user),
    signOut: () => {
      // Persona portraits are held as object URLs for the life of the page
      // (lib/authed-image-cache). Drop them with the session so a shared
      // machine keeps nothing of the signed-out account in memory.
      clearAuthedImageCache();
      void clerk.signOut();
    },
    manageAccount: () => clerk.openUserProfile(),
  };
}
