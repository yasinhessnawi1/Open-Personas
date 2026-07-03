"use client";

import { useCallback, useEffect, useReducer } from "react";
import { ApiError } from "@/lib/api/client";
import {
  type ConnectorLinkArtifact,
  connectFlowReducer,
  type FlowState,
  INITIAL_FLOW_STATE,
} from "./connect-flow-machine";

/** Map a thrown error to a stable reason code the UI renders in the honest voice. */
function reasonFrom(e: unknown): string {
  if (e instanceof ApiError) return e.code; // e.g. "connector_unavailable"
  return "error";
}

export interface UseConnectFlowArgs {
  /** Whether this platform is currently connected (derived from the list — the oracle). */
  connected: boolean;
  /** Issue a link artifact (the front-door proxy `POST /v1/me/connectors/{platform}/link`). */
  initiateLink: () => Promise<ConnectorLinkArtifact>;
  /** Re-fetch the connectors list — the ONLY completion signal (C6-D-1). Result unused here. */
  refresh: () => Promise<unknown>;
  /** Poll cadence while awaiting (default gentle 2s). */
  pollMs?: number;
  /** Injectable clock (ms) for deterministic expiry tests. */
  now?: () => number;
}

export interface UseConnectFlowResult {
  state: FlowState;
  /** Start a link (no-op if already initiating/awaiting — double-initiate suppressed). */
  initiate: () => void;
  /** Re-issue a fresh artifact after expiry (a fresh initiate, not a dead end). */
  reissue: () => void;
  /** Close/cancel — stops the poll (provably: the interval is cleared on the status change). */
  close: () => void;
}

/**
 * Spec C6 (T5) — drives the {@link connectFlowReducer} against real effects: the issue call,
 * the list-poll completion oracle, and the confirm-watch. The oracle is the LIST and only the
 * list — a gentle poll of `refresh()` with all four stop conditions: the binding appears
 * (`connected` flips), the server `expires_at` is reached (never a client constant — C6-D-8),
 * the dialog is closed (→ idle), or the interval effect unmounts. No zombie pollers: every
 * awaiting interval is cleared by its own effect cleanup.
 */
export function useConnectFlow({
  connected,
  initiateLink,
  refresh,
  pollMs = 2000,
  now = Date.now,
}: UseConnectFlowArgs): UseConnectFlowResult {
  const [state, dispatch] = useReducer(connectFlowReducer, INITIAL_FLOW_STATE);

  // Perform the issue call exactly once per entry into "initiating". A late resolution after
  // close is dropped (`active`) AND ignored by the reducer (ISSUED only applies to initiating).
  useEffect(() => {
    if (state.status !== "initiating") return;
    let active = true;
    void (async () => {
      try {
        const artifact = await initiateLink();
        if (active) dispatch({ type: "ISSUED", artifact });
      } catch (e) {
        if (active) dispatch({ type: "ISSUE_FAILED", reason: reasonFrom(e) });
      }
    })();
    return () => {
      active = false;
    };
  }, [state.status, initiateLink]);

  // The list-poll oracle: while awaiting, every pollMs check the server expiry then refresh.
  // Cleanup clears the interval on close / confirm / expire / unmount — the provable stop.
  useEffect(() => {
    if (state.status !== "awaiting") return;
    const deadline = state.artifact?.expires_at
      ? Date.parse(state.artifact.expires_at)
      : null;
    let active = true;
    const id = setInterval(() => {
      if (!active) return;
      if (deadline !== null && now() >= deadline) {
        dispatch({ type: "EXPIRED" });
        return;
      }
      // A transient poll failure (network blip / a 5xx) must NOT kill awaiting — swallow it
      // and let the next tick retry within the token's lifetime. The only terminal failure is
      // the issue call (→ `failed`); the poll only ever ends at binding / expiry / close.
      void refresh().catch(() => {});
    }, pollMs);
    return () => {
      active = false;
      clearInterval(id);
    };
  }, [state.status, state.artifact, refresh, pollMs, now]);

  // Confirmation: the binding appeared in the list. Only meaningful while awaiting (the
  // reducer guards it too), so an already-connected platform can't false-confirm a fresh flow.
  useEffect(() => {
    if (state.status === "awaiting" && connected) {
      dispatch({ type: "BINDING_SEEN" });
    }
  }, [state.status, connected]);

  const initiate = useCallback(() => dispatch({ type: "INITIATE" }), []);
  const reissue = useCallback(() => dispatch({ type: "REISSUE" }), []);
  const close = useCallback(() => dispatch({ type: "CLOSED" }), []);

  return { state, initiate, reissue, close };
}
