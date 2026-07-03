import type { components } from "@/lib/api/schema";

/**
 * Spec C6 (T5) — the ConnectFlow state machine (C6-D-1, the headline).
 *
 * ONE frame for all four mechanisms: `idle → initiating → awaiting → confirmed`, with
 * `failed(reason)` and `expired` branches. Only the middle *presentation* differs per
 * platform (the deep link / OAuth notice / code+destination — T6–T8); the transitions here
 * are platform-agnostic, which is exactly the §3 coherence property (proven by testing this
 * reducer once, independent of platform).
 *
 * Pure + total: every transition is a function of (state, event) with no side effects — the
 * hook (`useConnectFlow`) performs the issue call, the list-poll oracle, and the clock. The
 * reducer also encodes the **hygiene** guards: a second INITIATE while already
 * initiating/awaiting is a no-op (no duplicate artifact), and terminal events only apply
 * from the state they belong to (a stale ISSUED after CLOSED is ignored).
 */
export type ConnectorLinkArtifact =
  components["schemas"]["ConnectorLinkArtifact"];

export type FlowStatus =
  | "idle"
  | "initiating"
  | "awaiting"
  | "confirmed"
  | "failed"
  | "expired";

export interface FlowState {
  status: FlowStatus;
  /** The issued artifact (deep link / authorize URL / code + destination + expires_at). */
  artifact: ConnectorLinkArtifact | null;
  /** A failure reason code (mapped to honest-voice copy by the UI); null unless failed. */
  reason: string | null;
}

export type FlowEvent =
  | { type: "INITIATE" }
  | { type: "REISSUE" }
  | { type: "ISSUED"; artifact: ConnectorLinkArtifact }
  | { type: "ISSUE_FAILED"; reason: string }
  | { type: "BINDING_SEEN" }
  | { type: "EXPIRED" }
  | { type: "CLOSED" };

export const INITIAL_FLOW_STATE: FlowState = {
  status: "idle",
  artifact: null,
  reason: null,
};

/** The resting states a fresh initiate is allowed from (idle or after a prior end). */
function isResting(status: FlowStatus): boolean {
  return status === "idle" || status === "failed" || status === "expired";
}

export function connectFlowReducer(
  state: FlowState,
  event: FlowEvent,
): FlowState {
  switch (event.type) {
    case "INITIATE":
    case "REISSUE":
      // Hygiene: suppress double-initiate — a second click while initiating/awaiting must
      // NOT mint a second artifact. Only a resting state may (re)start.
      if (!isResting(state.status)) return state;
      return { status: "initiating", artifact: null, reason: null };

    case "ISSUED":
      // Ignore a late issue that resolved after the user closed (stale async).
      if (state.status !== "initiating") return state;
      return { status: "awaiting", artifact: event.artifact, reason: null };

    case "ISSUE_FAILED":
      if (state.status !== "initiating") return state;
      return { status: "failed", artifact: null, reason: event.reason };

    case "BINDING_SEEN":
      // Confirmation comes ONLY from the awaiting state (the list-poll oracle) — never a
      // stray true that would confirm a flow that never started.
      if (state.status !== "awaiting") return state;
      return { status: "confirmed", artifact: state.artifact, reason: null };

    case "EXPIRED":
      if (state.status !== "awaiting") return state;
      return { status: "expired", artifact: state.artifact, reason: null };

    case "CLOSED":
      return INITIAL_FLOW_STATE;

    default:
      return state;
  }
}
