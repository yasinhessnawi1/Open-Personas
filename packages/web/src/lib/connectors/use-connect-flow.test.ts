import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ConnectorLinkArtifact } from "./connect-flow-machine";
import { useConnectFlow } from "./use-connect-flow";

/**
 * Spec C6 (T5) — the ConnectFlow hook's side effects: exactly-once issue (double-initiate
 * suppressed), the list-poll oracle, server-authoritative expiry, confirm-on-binding, and —
 * the classic bug in this shape — NO zombie poller after unmount/close.
 */

const BASE = Date.parse("2026-07-03T12:00:00Z");

function artifact(
  over: Partial<ConnectorLinkArtifact> = {},
): ConnectorLinkArtifact {
  return {
    code: "ABCD1234",
    destination: "+15551234567",
    // Far-future expiry so tests that aren't about expiry never trip it (fake timers start
    // at the real wall clock, so a same-day expiry could already be past).
    expires_at: "2099-01-01T00:00:00+00:00",
    deep_link: null,
    authorize_url: null,
    ...over,
  };
}

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("useConnectFlow", () => {
  it("issues exactly once even if initiate is clicked twice (no duplicate artifact)", async () => {
    const initiateLink = vi.fn().mockResolvedValue(artifact());
    const refresh = vi.fn().mockResolvedValue(undefined);
    const { result } = renderHook(() =>
      useConnectFlow({ connected: false, initiateLink, refresh }),
    );

    await act(async () => {
      result.current.initiate();
      result.current.initiate();
    });

    expect(initiateLink).toHaveBeenCalledTimes(1);
    expect(result.current.state.status).toBe("awaiting");
  });

  it("polls the list while awaiting and confirms when the binding appears", async () => {
    vi.useFakeTimers();
    const initiateLink = vi.fn().mockResolvedValue(artifact());
    const refresh = vi.fn().mockResolvedValue(undefined);
    const { result, rerender } = renderHook(
      ({ connected }: { connected: boolean }) =>
        useConnectFlow({ connected, initiateLink, refresh, pollMs: 2000 }),
      { initialProps: { connected: false } },
    );

    await act(async () => {
      result.current.initiate();
    });
    expect(result.current.state.status).toBe("awaiting");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(refresh).toHaveBeenCalled(); // the oracle polled the list

    // The binding now appears in the list → confirmed (the list is the ONLY success signal).
    rerender({ connected: true });
    await act(async () => {});
    expect(result.current.state.status).toBe("confirmed");
  });

  it("a transient poll failure does NOT kill awaiting — it retries within expiry (bar 2)", async () => {
    vi.useFakeTimers();
    const initiateLink = vi.fn().mockResolvedValue(artifact());
    const refresh = vi
      .fn()
      .mockRejectedValueOnce(new Error("network blip")) // transient
      .mockResolvedValue(undefined);
    const { result, rerender } = renderHook(
      ({ connected }: { connected: boolean }) =>
        useConnectFlow({ connected, initiateLink, refresh, pollMs: 2000 }),
      { initialProps: { connected: false } },
    );

    await act(async () => {
      result.current.initiate();
    });
    expect(result.current.state.status).toBe("awaiting");

    // A tick whose refresh rejects — awaiting must survive (transient, retry next tick).
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(result.current.state.status).toBe("awaiting");

    // The retry lands the binding → confirmed (the flow recovered from the blip).
    rerender({ connected: true });
    await act(async () => {});
    expect(result.current.state.status).toBe("confirmed");
  });

  it("expires at the server expires_at, not a client constant (C6-D-8)", async () => {
    vi.useFakeTimers();
    let nowMs = BASE;
    const initiateLink = vi
      .fn()
      .mockResolvedValue(artifact({ expires_at: "2026-07-03T12:00:05+00:00" })); // +5s
    const refresh = vi.fn().mockResolvedValue(undefined);
    const { result } = renderHook(() =>
      useConnectFlow({
        connected: false,
        initiateLink,
        refresh,
        pollMs: 2000,
        now: () => nowMs,
      }),
    );

    await act(async () => {
      result.current.initiate();
    });
    expect(result.current.state.status).toBe("awaiting");

    nowMs = BASE + 6000; // past the server deadline
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(result.current.state.status).toBe("expired");
  });

  it("re-issue after expiry shows the NEW artifact, never the stale code (bar 3)", async () => {
    vi.useFakeTimers();
    let nowMs = BASE;
    const initiateLink = vi
      .fn()
      .mockResolvedValueOnce(
        artifact({ code: "OLD00000", expires_at: "2026-07-03T12:00:05+00:00" }),
      )
      .mockResolvedValueOnce(artifact({ code: "NEW11111" }));
    const refresh = vi.fn().mockResolvedValue(undefined);
    const { result } = renderHook(() =>
      useConnectFlow({
        connected: false,
        initiateLink,
        refresh,
        pollMs: 2000,
        now: () => nowMs,
      }),
    );

    await act(async () => {
      result.current.initiate();
    });
    expect(result.current.state.artifact?.code).toBe("OLD00000");

    nowMs = BASE + 6000; // past the OLD code's expiry
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(result.current.state.status).toBe("expired");

    await act(async () => {
      result.current.reissue();
    });
    // The dead OLD code is gone; only the fresh one is shown.
    expect(result.current.state.status).toBe("awaiting");
    expect(result.current.state.artifact?.code).toBe("NEW11111");
  });

  it("stops polling on unmount — no zombie interval keeps hitting the API", async () => {
    vi.useFakeTimers();
    const initiateLink = vi.fn().mockResolvedValue(artifact());
    const refresh = vi.fn().mockResolvedValue(undefined);
    const { result, unmount } = renderHook(() =>
      useConnectFlow({ connected: false, initiateLink, refresh, pollMs: 2000 }),
    );

    await act(async () => {
      result.current.initiate();
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    const callsBeforeUnmount = refresh.mock.calls.length;
    expect(callsBeforeUnmount).toBeGreaterThan(0);

    unmount();
    await vi.advanceTimersByTimeAsync(20000);
    expect(refresh.mock.calls.length).toBe(callsBeforeUnmount); // no calls after unmount
  });

  it("close stops the poll too (idle → interval cleared)", async () => {
    vi.useFakeTimers();
    const initiateLink = vi.fn().mockResolvedValue(artifact());
    const refresh = vi.fn().mockResolvedValue(undefined);
    const { result } = renderHook(() =>
      useConnectFlow({ connected: false, initiateLink, refresh, pollMs: 2000 }),
    );
    await act(async () => {
      result.current.initiate();
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    const before = refresh.mock.calls.length;

    await act(async () => {
      result.current.close();
    });
    expect(result.current.state.status).toBe("idle");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(20000);
    });
    expect(refresh.mock.calls.length).toBe(before); // no calls after close
  });
});
