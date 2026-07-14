"use client";

/**
 * R9-050 — "Turn into file" persistent-loading fix.
 *
 * R9-025b fired the `file_extract` job and showed an OPTIMISTIC toast that
 * vanished immediately — well before the durable job (an LLM extraction pass
 * + a sandbox render, genuinely several seconds) actually produced a file —
 * a dead gap that read as "broken/dead" (owner re-test, 2026-07-14). This
 * hook keeps the toast ALIVE (a sonner `loading` toast, id-addressable via
 * `useNotify().notifyLoading`) until the file actually lands, then swaps it
 * to a success/error state in place.
 *
 * **Completion-signal decision — investigated three options:**
 *   1. A job-status GET endpoint. Does not exist: there is no `/v1/jobs/{id}`
 *      route anywhere in the api. `job_id` DOES round-trip in the 202 body
 *      (`TurnIntoFileResponse`), but there is nowhere to ask it "are you
 *      done yet".
 *   2. The `sidebar.changed` SSE event the job publishes on success
 *      (`file_extract.py`'s "refresh-signal decision", reusing
 *      `publish_sidebar_changed`). Its OWN contract
 *      (`persona_api/realtime/events.py`'s `SidebarChangedEvent` docstring)
 *      states: "`reason` names the originating mutation for observability
 *      only; the client MUST NOT branch on it" — and the payload carries no
 *      conversation/message/job id at all (A11-D-2's "surface-lags-truth":
 *      every consumer in this app already treats a data frame as a bare
 *      refetch trigger, never as state, per `me-events-router.ts`). There is
 *      therefore no honest way to key a PER-JOB loading state to this event.
 *   3. A bounded poll of the SAME resource the Files panel itself reads
 *      (`GET /v1/personas/{persona_id}/artifacts?conversation_id=`, the
 *      `useConversationArtifacts` hook — the doc-gen sidecar the job writes
 *      lands there) for a NEW `ref` absent from a pre-fire baseline
 *      snapshot. This mirrors this app's own precedent for the identical
 *      problem shape — `use-persona-avatar-poll.ts` (a strictly-bounded,
 *      leak-safe poll for a different async background job's effect landing
 *      on a known resource) — same cadence/ceiling discipline, adapted to a
 *      list instead of a single field.
 *
 * (3) is what this hook does. A fresh `ref` appearing during the poll window
 * is treated as "the job landed": the handler writes exactly one new
 * artifact per call and the route's enqueue is idempotent per
 * `(message_id, format)` (`file_extract_idempotency_key`), so this is a
 * correct signal for the ordinary case. The one trade-off: a concurrent
 * manual upload to the SAME conversation during the ~90s window would also
 * read as "landed" — accepted rather than adding job-identity plumbing to
 * `file_extract`'s response/event shape, which is out of this fix's lean
 * scope (flagged here for a future tightening, not silently swallowed).
 *
 * On the immediate POST failing (422 wrong role, 503 unavailable), the toast
 * swaps straight to an error — no poll starts. On the poll exhausting its
 * ceiling without a new artifact, the toast swaps to an honest "still
 * working" message rather than a false error — the job may simply be slow
 * (LLM + sandbox cold start) and its own retry ladder is still live.
 */

import { useTranslations } from "next-intl";
import { useCallback, useEffect, useRef } from "react";
import { useAuth } from "@/auth";
import { useNotify } from "@/components/providers/notification-provider";
import { createApiClient } from "@/lib/api/client";
import { notifyConversationFilesChanged } from "@/lib/hooks/use-conversation-artifacts";
import type { TurnIntoFileFormat } from "@/lib/turn-into-file";
import { turnMessageIntoFile } from "@/lib/turn-into-file";

const TEMPLATE = process.env.NEXT_PUBLIC_CLERK_JWT_TEMPLATE;

/**
 * Poll cadence + hard ceiling — mirrors `use-persona-avatar-poll.ts`'s own
 * constants/rationale: frequent enough to catch completion within a poll or
 * two, bounded so a stuck/failed job can never poll forever.
 */
export const TURN_INTO_FILE_POLL_INTERVAL_MS = 2500;
export const TURN_INTO_FILE_POLL_MAX_MS = 90_000;

export interface UseTurnIntoFileOptions {
  conversationId: string;
  personaId: string;
  /** Test-only overrides so a poll-to-timeout doesn't need real minutes. */
  pollIntervalMs?: number;
  pollMaxMs?: number;
}

export type TurnIntoFileHandler = (
  assistantMessageId: string,
  format: TurnIntoFileFormat,
) => void;

export function useTurnIntoFile({
  conversationId,
  personaId,
  pollIntervalMs = TURN_INTO_FILE_POLL_INTERVAL_MS,
  pollMaxMs = TURN_INTO_FILE_POLL_MAX_MS,
}: UseTurnIntoFileOptions): TurnIntoFileHandler {
  const t = useTranslations("chat");
  const { getToken } = useAuth();
  const { notify, notifyLoading } = useNotify();

  // Refs so the poll loop always reads the LATEST token-getter without
  // re-subscribing (mirrors use-persona-avatar-poll.ts's own `getTokenRef`
  // discipline — a non-memoising auth host must never restart an in-flight
  // poll by churning a callback identity).
  const getTokenRef = useRef(getToken);
  getTokenRef.current = getToken;

  const mountedRef = useRef(true);
  useEffect(
    () => () => {
      mountedRef.current = false;
    },
    [],
  );

  // (message_id, format) currently in flight — mirrors the backend's own
  // idempotency key (`file_extract_idempotency_key`) so a double-click is a
  // client-side no-op exactly where the server would have no-op'd anyway,
  // instead of stacking a second loading toast for the same job.
  const inFlight = useRef<Set<string>>(new Set());

  const fetchArtifactRefs = useCallback(async (): Promise<Set<string>> => {
    const jwt = await getTokenRef.current(
      TEMPLATE ? { template: TEMPLATE } : undefined,
    );
    const client = createApiClient(() => Promise.resolve(jwt));
    const res = await client.GET("/v1/personas/{persona_id}/artifacts", {
      params: {
        path: { persona_id: personaId },
        query: { conversation_id: conversationId },
      },
    });
    return new Set((res.data?.items ?? []).map((item) => item.ref));
  }, [personaId, conversationId]);

  const handleTurnIntoFile = useCallback<TurnIntoFileHandler>(
    (assistantMessageId, format) => {
      const key = `${assistantMessageId}:${format}`;
      if (inFlight.current.has(key)) return; // already loading — idempotent no-op
      inFlight.current.add(key);
      const finish = () => {
        inFlight.current.delete(key);
      };

      const toastId = notifyLoading(t("actions.turnIntoFile.toast"));

      const pollUntilLanded = async (baseline: Set<string>) => {
        let elapsed = 0;
        for (;;) {
          if (!mountedRef.current) return; // unmounted — stop silently, no stray toast
          await new Promise((resolve) => setTimeout(resolve, pollIntervalMs));
          elapsed += pollIntervalMs;
          if (!mountedRef.current) return;
          try {
            const refs = await fetchArtifactRefs();
            const landed = [...refs].some((ref) => !baseline.has(ref));
            if (landed) {
              notify({
                level: "success",
                title: t("actions.turnIntoFile.successToast"),
                id: toastId,
              });
              notifyConversationFilesChanged(); // an already-open Files panel picks it up live
              finish();
              return;
            }
          } catch {
            // Transient read failure — keep polling to the ceiling rather
            // than declaring a false error over a network hiccup.
          }
          if (elapsed >= pollMaxMs) {
            notify({
              level: "warning",
              title: t("actions.turnIntoFile.timeoutToast"),
              id: toastId,
              persist: false,
            });
            finish();
            return;
          }
        }
      };

      void (async () => {
        // Snapshot the baseline CONCURRENTLY with the POST — never delay the
        // fire-immediately UX (R9-025b's one-click contract) for it; a
        // baseline-fetch failure fails soft to an empty set (still detects
        // ANY artifact landing, just less precisely).
        const baselinePromise = fetchArtifactRefs().catch(
          () => new Set<string>(),
        );
        try {
          await turnMessageIntoFile(
            conversationId,
            assistantMessageId,
            format,
            () =>
              getTokenRef.current(
                TEMPLATE ? { template: TEMPLATE } : undefined,
              ),
          );
        } catch {
          notify({
            level: "error",
            title: t("actions.turnIntoFile.errorToast"),
            id: toastId,
          });
          finish();
          return;
        }
        const baseline = await baselinePromise;
        void pollUntilLanded(baseline);
      })();
    },
    [
      conversationId,
      fetchArtifactRefs,
      notify,
      notifyLoading,
      pollIntervalMs,
      pollMaxMs,
      t,
    ],
  );

  return handleTurnIntoFile;
}
