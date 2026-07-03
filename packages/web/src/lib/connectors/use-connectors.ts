"use client";

import { useCallback, useEffect, useState } from "react";
import { useAuth } from "@/auth";
import { createApiClient, unwrap } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";

const TEMPLATE = process.env.NEXT_PUBLIC_CLERK_JWT_TEMPLATE;

/** One active platform connection (Spec C6, `GET /v1/me/connectors`). */
export type ConnectorConnection =
  components["schemas"]["ConnectorConnectionOut"];

export interface UseConnectorsState {
  /** The caller's active bindings (own-only; RLS-scoped server-side). */
  connections: ConnectorConnection[];
  /** True until the first fetch resolves. */
  loading: boolean;
  error: Error | null;
  /**
   * Re-fetch the list — the connect flows' completion oracle (C6-D-1). Returns the fresh
   * connections so a caller (the OAuth return, T7) can key its toast off the ACTUAL list
   * rather than stale React state; returns `[]` on error.
   */
  refresh: () => Promise<ConnectorConnection[]>;
}

/**
 * Spec C6 — the connectors list state (the surface's data + the flows' completion oracle).
 *
 * Plain `useState` + `fetch` (the house pattern; TanStack Query never landed, D-09-5) —
 * mirrors `useConversationDocuments`. Reads `GET /v1/me/connectors` with the caller's Clerk
 * token; `refresh()` is exposed so a connect flow can poll until the binding appears and a
 * disconnect can re-sync (C6-D-1: the list is the source of truth, not a UI chip).
 */
export function useConnectors(): UseConnectorsState {
  const { getToken } = useAuth();
  const [connections, setConnections] = useState<ConnectorConnection[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);

  const token = useCallback(
    () => getToken(TEMPLATE ? { template: TEMPLATE } : undefined),
    [getToken],
  );

  const refresh = useCallback(async (): Promise<ConnectorConnection[]> => {
    setError(null);
    try {
      const jwt = await token();
      const client = createApiClient(() => Promise.resolve(jwt));
      const rows = await unwrap(await client.GET("/v1/me/connectors"));
      setConnections(rows);
      return rows;
    } catch (e) {
      // Surface the error so the manager can offer retry (honest voice, criterion 7);
      // leave the last-known list in place rather than blanking the surface.
      setError(e instanceof Error ? e : new Error(String(e)));
      return [];
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return { connections, loading, error, refresh };
}
