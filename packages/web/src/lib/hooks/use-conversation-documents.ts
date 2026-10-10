"use client";

import { useCallback, useEffect, useState } from "react";
import { useAuth } from "@/auth";
import { useMeEvent } from "@/components/providers/me-events-provider";
import { ApiError, createApiClient, unwrap } from "@/lib/api/client";
import type { DocumentRef } from "@/lib/upload";

const TEMPLATE = process.env.NEXT_PUBLIC_CLERK_JWT_TEMPLATE;

/**
 * F3 (T12) — conversation document list state.
 *
 * Reads via `GET /v1/conversations/:id/documents` (Spec 14 T18); exposes
 * optimistic append/remove handlers so T13 + T14 can refresh the panel
 * without a server round-trip. Conversation-scoped — distinct lifecycle
 * from `useChat`'s message-scoped state (D-F3-X-cap-attached-state-on-
 * conversation-switch ensures both reset on conversationId change).
 *
 * Mirrors `useChat`'s plain useState + fetch pattern (the Phase 1 D-09-5
 * note about TanStack Query never landed as a dep). One refresh path,
 * one optimistic path; the panel re-renders on every state change.
 */
export interface ConversationDocumentsState {
  documents: DocumentRef[];
  loading: boolean;
  error: Error | null;
  /** Replace the list with the server's view (call after a successful upload). */
  refresh: () => Promise<void>;
  /** Append a newly-uploaded ref optimistically (server confirms on next refresh). */
  addOptimistic: (ref: DocumentRef) => void;
  /** Drop a ref from the list optimistically (e.g. on remove click before server returns). */
  removeOptimistic: (docRef: string) => void;
}

export function useConversationDocuments(
  conversationId: string,
): ConversationDocumentsState {
  const { getToken } = useAuth();
  const [documents, setDocuments] = useState<DocumentRef[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);

  const token = useCallback(
    () => getToken(TEMPLATE ? { template: TEMPLATE } : undefined),
    [getToken],
  );

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const jwt = await token();
      const client = createApiClient(() => Promise.resolve(jwt));
      const refs = await unwrap(
        await client.GET("/v1/conversations/{conversation_id}/documents", {
          params: { path: { conversation_id: conversationId } },
        }),
      );
      setDocuments(refs);
    } catch (e) {
      // Surface ApiError so the panel can show retry; otherwise log and
      // leave the list empty (avoid breaking the chat surface on transient
      // network errors).
      setError(
        e instanceof Error
          ? e
          : new Error(e instanceof ApiError ? e.message : String(e)),
      );
    } finally {
      setLoading(false);
    }
  }, [conversationId, token]);

  // Fetch on mount + on conversationId change (the sole dep, per
  // D-F3-X-cap-attached-state-on-conversation-switch).
  useEffect(() => {
    void refresh();
  }, [refresh]);

  // D-FV0-31: the background document index publishes `sidebar.changed`
  // (reason=document.indexed) when a document's index finishes, so a chip that
  // read "Indexing…" updates without reopening the conversation.
  useMeEvent("sidebar.changed", () => void refresh());

  const addOptimistic = useCallback((ref: DocumentRef) => {
    setDocuments((prev) =>
      prev.some((d) => d.doc_ref === ref.doc_ref) ? prev : [...prev, ref],
    );
  }, []);

  const removeOptimistic = useCallback((docRef: string) => {
    setDocuments((prev) => prev.filter((d) => d.doc_ref !== docRef));
  }, []);

  return {
    documents,
    loading,
    error,
    refresh,
    addOptimistic,
    removeOptimistic,
  };
}
