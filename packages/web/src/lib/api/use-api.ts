"use client";

import { useMemo } from "react";
import { useAuth } from "@/auth";
import { ACCOUNT_DELETED_PATH, createApiClient } from "./client";

const TEMPLATE = process.env.NEXT_PUBLIC_CLERK_JWT_TEMPLATE;

/**
 * A typed API client for Client Components / TanStack Query hooks. Injects the
 * caller's Clerk JWT-template token as `Authorization: Bearer`. Re-created when
 * the Clerk auth context changes. (D-09-1, D-09-2.)
 */
export function useApi() {
  const { getToken } = useAuth();
  return useMemo(
    () =>
      createApiClient(
        () => getToken(TEMPLATE ? { template: TEMPLATE } : undefined),
        // R9-284 T9 (review L6): an erased account's session leaves for the goodbye page.
        () => window.location.assign(ACCOUNT_DELETED_PATH),
      ),
    [getToken],
  );
}
