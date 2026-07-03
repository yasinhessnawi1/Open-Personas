import { describe, expect, it } from "vitest";
import {
  type ConnectorLinkArtifact,
  connectFlowReducer,
  type FlowState,
  INITIAL_FLOW_STATE,
} from "./connect-flow-machine";

/**
 * Spec C6 (T5) — the ConnectFlow machine, tested once and platform-agnostically (the §3
 * coherence property: the same transitions frame every mechanism). Covers the hygiene guards
 * (no double-initiate; no stale/false transitions) that the whole flow's correctness rests on.
 */

const ARTIFACT: ConnectorLinkArtifact = {
  code: "ABCD1234",
  destination: "+15551234567",
  expires_at: "2026-07-03T12:10:00+00:00",
  deep_link: null,
  authorize_url: null,
};

function at(
  status: FlowState["status"],
  over: Partial<FlowState> = {},
): FlowState {
  return { status, artifact: null, reason: null, ...over };
}

describe("connectFlowReducer", () => {
  it("INITIATE starts from a resting state (idle/failed/expired)", () => {
    for (const resting of ["idle", "failed", "expired"] as const) {
      expect(connectFlowReducer(at(resting), { type: "INITIATE" }).status).toBe(
        "initiating",
      );
    }
  });

  it("suppresses double-initiate while initiating or awaiting (no second artifact)", () => {
    const initiating = at("initiating");
    expect(connectFlowReducer(initiating, { type: "INITIATE" })).toBe(
      initiating,
    );
    const awaiting = at("awaiting", { artifact: ARTIFACT });
    expect(connectFlowReducer(awaiting, { type: "INITIATE" })).toBe(awaiting);
    // REISSUE is likewise suppressed mid-flight.
    expect(connectFlowReducer(awaiting, { type: "REISSUE" })).toBe(awaiting);
  });

  it("ISSUED moves initiating → awaiting and stores the artifact", () => {
    const next = connectFlowReducer(at("initiating"), {
      type: "ISSUED",
      artifact: ARTIFACT,
    });
    expect(next).toEqual(at("awaiting", { artifact: ARTIFACT }));
  });

  it("ignores a stale ISSUED that resolved after close (not from initiating)", () => {
    const idle = INITIAL_FLOW_STATE;
    expect(
      connectFlowReducer(idle, { type: "ISSUED", artifact: ARTIFACT }),
    ).toBe(idle);
  });

  it("ISSUE_FAILED moves initiating → failed with the reason", () => {
    const next = connectFlowReducer(at("initiating"), {
      type: "ISSUE_FAILED",
      reason: "connector_unavailable",
    });
    expect(next).toEqual(at("failed", { reason: "connector_unavailable" }));
  });

  it("BINDING_SEEN confirms ONLY from awaiting (no false confirm from idle)", () => {
    const awaiting = at("awaiting", { artifact: ARTIFACT });
    expect(connectFlowReducer(awaiting, { type: "BINDING_SEEN" })).toEqual(
      at("confirmed", { artifact: ARTIFACT }),
    );
    const idle = INITIAL_FLOW_STATE;
    expect(connectFlowReducer(idle, { type: "BINDING_SEEN" })).toBe(idle);
  });

  it("EXPIRED applies only from awaiting", () => {
    const awaiting = at("awaiting", { artifact: ARTIFACT });
    expect(connectFlowReducer(awaiting, { type: "EXPIRED" }).status).toBe(
      "expired",
    );
    const confirmed = at("confirmed", { artifact: ARTIFACT });
    expect(connectFlowReducer(confirmed, { type: "EXPIRED" })).toBe(confirmed);
  });

  it("CLOSED resets to the initial state from anywhere", () => {
    for (const s of [
      "initiating",
      "awaiting",
      "confirmed",
      "failed",
      "expired",
    ] as const) {
      expect(
        connectFlowReducer(at(s, { artifact: ARTIFACT }), { type: "CLOSED" }),
      ).toEqual(INITIAL_FLOW_STATE);
    }
  });

  it("expired → REISSUE restarts AND drops the stale artifact (no dead code survives)", () => {
    const next = connectFlowReducer(at("expired", { artifact: ARTIFACT }), {
      type: "REISSUE",
    });
    expect(next.status).toBe("initiating");
    // The old code is gone the instant re-issue begins — it can never linger to be re-read.
    expect(next.artifact).toBeNull();
  });
});
