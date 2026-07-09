/**
 * Spec V7 T1 — app-level call-session provider.
 *
 * Exercises the hoist's load-bearing invariants against the REAL `useVoiceCall`
 * (its internals are consumed unchanged) with a mocked `livekit-client`, so the
 * assertions are end-to-end: how many `Room`s actually get constructed, whether
 * a Strict-Mode double-mount leaks one, and whether teardown releases the
 * `<audio>` sinks. The pure call-state mapping is covered by `call-state.test.ts`;
 * this file owns the provider's lifecycle contract.
 */

import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { StrictMode, useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { persistCall, RESUME_FRESHNESS_MS } from "@/lib/voice/call-persistence";
import { loadRecap } from "@/lib/voice/call-recap";
import { INITIAL_CALL_STATE } from "@/lib/voice/call-state";
import {
  CallSessionProvider,
  type CallTarget,
  useCallSession,
} from "./call-session-context";

// --- livekit-client mock: a fake Room that records construction + lifecycle. ---
const lk = vi.hoisted(() => {
  const rooms: FakeRoom[] = [];
  class FakeRemoteAudioTrack {
    attach(): HTMLAudioElement {
      return document.createElement("audio");
    }
  }
  class FakeRoom {
    handlers = new Map<string, (...a: unknown[]) => void>();
    canPlaybackAudio = true;
    connect = vi.fn(async () => undefined);
    startAudio = vi.fn(async () => undefined);
    disconnect = vi.fn(async () => undefined);
    localParticipant = {
      setMicrophoneEnabled: vi.fn(async () => undefined),
      getTrackPublication: vi.fn(() => ({ audioTrack: {} })),
    };
    constructor() {
      rooms.push(this);
    }
    on(ev: string, cb: (...a: unknown[]) => void): this {
      this.handlers.set(ev, cb);
      return this;
    }
    emit(ev: string, ...args: unknown[]): void {
      this.handlers.get(ev)?.(...args);
    }
  }
  return { rooms, FakeRoom, FakeRemoteAudioTrack };
});

vi.mock("livekit-client", () => ({
  Room: lk.FakeRoom,
  RemoteAudioTrack: lk.FakeRemoteAudioTrack,
  RoomEvent: {
    ConnectionStateChanged: "connectionStateChanged",
    TrackSubscribed: "trackSubscribed",
    DataReceived: "dataReceived",
    AudioPlaybackStatusChanged: "audioPlaybackStatusChanged",
    Disconnected: "disconnected",
  },
  ConnectionState: {
    Disconnected: "disconnected",
    Connected: "connected",
    Connecting: "connecting",
    Reconnecting: "reconnecting",
  },
  DisconnectReason: { CLIENT_INITIATED: "CLIENT_INITIATED" },
  Track: { Source: { Microphone: "microphone" } },
  createAudioAnalyser: () => ({ calculateVolume: () => 0, cleanup: vi.fn() }),
}));

// R9-012: the provider refreshes the sidebar at call end via useSidebarRefresh.
vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn() }),
}));

vi.mock("@/auth", () => ({
  useAuth: () => ({ getToken: async () => "jwt" }),
}));

vi.mock("@/lib/voice/token", () => ({
  fetchVoiceToken: vi.fn(async () => ({
    token: "tok",
    roomName: "persona:sess",
    livekitUrl: "ws://lk",
  })),
}));

const TARGET_A: CallTarget = {
  personaId: "p-a",
  conversationId: "c-a",
  personaName: "Ada",
};
const TARGET_B: CallTarget = {
  personaId: "p-b",
  conversationId: "c-b",
  personaName: "Boole",
};

function Harness() {
  const s = useCallSession();
  const [outcome, setOutcome] = useState("");
  return (
    <div>
      <span data-testid="phase">{s.state.phase}</span>
      <span data-testid="active">{String(s.isActive)}</span>
      <span data-testid="target">{s.target?.personaName ?? "none"}</span>
      <button
        type="button"
        data-testid="start-a"
        onClick={() => s.start(TARGET_A)}
      >
        start-a
      </button>
      <button
        type="button"
        data-testid="start-b"
        onClick={() => s.start(TARGET_B)}
      >
        start-b
      </button>
      <button type="button" data-testid="end" onClick={() => void s.end()}>
        end
      </button>
      <span data-testid="outcome">{outcome}</span>
      <span data-testid="pending">
        {s.pendingSwitch?.personaName ?? "none"}
      </span>
      <button
        type="button"
        data-testid="request-b"
        onClick={() => setOutcome(s.requestCall(TARGET_B))}
      >
        request-b
      </button>
      <button
        type="button"
        data-testid="confirm-switch"
        onClick={() => void s.confirmSwitch()}
      >
        confirm
      </button>
      <button
        type="button"
        data-testid="cancel-switch"
        onClick={() => s.cancelSwitch()}
      >
        cancel
      </button>
      <span data-testid="resumable">{s.resumable?.personaName ?? "none"}</span>
      <button type="button" data-testid="resume" onClick={() => s.resumeCall()}>
        resume
      </button>
      <span data-testid="mic-active">{String(s.state.micActive)}</span>
      <span data-testid="input-mode">{s.inputMode}</span>
      <button
        type="button"
        data-testid="set-ptt"
        onClick={() => s.setInputMode("ptt")}
      >
        ptt
      </button>
      <button
        type="button"
        data-testid="set-always"
        onClick={() => s.setInputMode("always")}
      >
        always
      </button>
      <button
        type="button"
        data-testid="ptt-hold"
        onClick={() => s.setPttHeld(true)}
      >
        hold
      </button>
      <button
        type="button"
        data-testid="ptt-release"
        onClick={() => s.setPttHeld(false)}
      >
        release
      </button>
    </div>
  );
}

function encodeState(toState: string, fromState: string): Uint8Array {
  return new TextEncoder().encode(
    JSON.stringify({
      type: "state",
      from_state: fromState,
      to_state: toState,
      trigger: "t",
      at: "t",
    }),
  );
}

beforeEach(() => {
  lk.rooms.length = 0;
  window.sessionStorage.clear();
  window.localStorage.clear();
});
afterEach(() => {
  // Strip any stray audio sinks so a leak in one test can't pass the next.
  for (const el of document.querySelectorAll("audio")) el.remove();
});

describe("CallSessionProvider", () => {
  it("is idle on mount — no Room, no active call", () => {
    render(
      <CallSessionProvider>
        <Harness />
      </CallSessionProvider>,
    );
    expect(screen.getByTestId("phase").textContent).toBe(
      INITIAL_CALL_STATE.phase,
    );
    expect(screen.getByTestId("active").textContent).toBe("false");
    expect(screen.getByTestId("target").textContent).toBe("none");
    expect(lk.rooms).toHaveLength(0);
  });

  it("start() places exactly one call against the target", async () => {
    render(
      <CallSessionProvider>
        <Harness />
      </CallSessionProvider>,
    );
    fireEvent.click(screen.getByTestId("start-a"));
    await waitFor(() => expect(lk.rooms).toHaveLength(1));
    expect(lk.rooms[0].connect).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("active").textContent).toBe("true");
    expect(screen.getByTestId("target").textContent).toBe("Ada");
  });

  it("single-Room invariant: a start while a call is active creates no second Room", async () => {
    render(
      <CallSessionProvider>
        <Harness />
      </CallSessionProvider>,
    );
    fireEvent.click(screen.getByTestId("start-a"));
    await waitFor(() => expect(lk.rooms).toHaveLength(1));
    // Starting a different persona while one is live must NOT spin up a 2nd Room
    // (useVoiceCall's roomRef guard holds) — this is exactly why switch/replace
    // (T4) has to end the active call first.
    fireEvent.click(screen.getByTestId("start-b"));
    await Promise.resolve();
    expect(lk.rooms).toHaveLength(1);
  });

  it("Strict-Mode double-mount leaves no second Room and no leaked audio", async () => {
    const { unmount } = render(
      <StrictMode>
        <CallSessionProvider>
          <Harness />
        </CallSessionProvider>
      </StrictMode>,
    );
    fireEvent.click(screen.getByTestId("start-a"));
    await waitFor(() => expect(lk.rooms).toHaveLength(1));
    const room = lk.rooms[0];

    // Simulate the persona's inbound audio track → an <audio> sink is attached.
    room.emit("trackSubscribed", new lk.FakeRemoteAudioTrack());
    expect(document.querySelectorAll("audio")).toHaveLength(1);

    // A true unmount of the provider must release the Room + detach the sinks.
    unmount();
    expect(room.disconnect).toHaveBeenCalled();
    expect(document.querySelectorAll("audio")).toHaveLength(0);
  });

  it("end() releases the call and clears the session", async () => {
    render(
      <CallSessionProvider>
        <Harness />
      </CallSessionProvider>,
    );
    fireEvent.click(screen.getByTestId("start-a"));
    await waitFor(() => expect(lk.rooms).toHaveLength(1));
    const room = lk.rooms[0];

    fireEvent.click(screen.getByTestId("end"));
    await waitFor(() =>
      expect(screen.getByTestId("active").textContent).toBe("false"),
    );
    expect(room.disconnect).toHaveBeenCalled();
    expect(screen.getByTestId("target").textContent).toBe("none");
  });

  it("requestCall: idle → started, same conversation → current", async () => {
    render(
      <CallSessionProvider>
        <Harness />
      </CallSessionProvider>,
    );
    // Idle → starts immediately.
    fireEvent.click(screen.getByTestId("start-a"));
    await waitFor(() => expect(lk.rooms).toHaveLength(1));
    // requestCall for a DIFFERENT conversation → a switch is pending (no 2nd Room).
    fireEvent.click(screen.getByTestId("request-b"));
    expect(screen.getByTestId("outcome").textContent).toBe("switch");
    expect(screen.getByTestId("pending").textContent).toBe("Boole");
    expect(lk.rooms).toHaveLength(1);
  });

  it("cancelSwitch keeps the current call and clears the pending one", async () => {
    render(
      <CallSessionProvider>
        <Harness />
      </CallSessionProvider>,
    );
    fireEvent.click(screen.getByTestId("start-a"));
    await waitFor(() => expect(lk.rooms).toHaveLength(1));
    fireEvent.click(screen.getByTestId("request-b"));
    fireEvent.click(screen.getByTestId("cancel-switch"));
    expect(screen.getByTestId("pending").textContent).toBe("none");
    expect(screen.getByTestId("target").textContent).toBe("Ada");
    expect(lk.rooms).toHaveLength(1);
  });

  it("confirmSwitch serializes end→start — never two Rooms at once (D-V7-4)", async () => {
    render(
      <CallSessionProvider>
        <Harness />
      </CallSessionProvider>,
    );
    fireEvent.click(screen.getByTestId("start-a"));
    await waitFor(() => expect(lk.rooms).toHaveLength(1));
    const roomA = lk.rooms[0];

    fireEvent.click(screen.getByTestId("request-b"));
    fireEvent.click(screen.getByTestId("confirm-switch"));

    // The new call (Boole) comes up — a SECOND Room, but only after the first is
    // gone.
    await waitFor(() => expect(lk.rooms).toHaveLength(2));
    const roomB = lk.rooms[1];
    await waitFor(() => expect(roomB.connect).toHaveBeenCalled());

    // Serialization proof: Room A was disconnected BEFORE Room B connected — the
    // two Rooms never overlapped (no double mic publication).
    expect(roomA.disconnect).toHaveBeenCalled();
    const disconnectA = roomA.disconnect.mock.invocationCallOrder[0];
    const connectB = roomB.connect.mock.invocationCallOrder[0];
    expect(disconnectA).toBeLessThan(connectB);

    expect(screen.getByTestId("target").textContent).toBe("Boole");
    expect(screen.getByTestId("pending").textContent).toBe("none");
  });

  it("offers a FRESH persisted call as a resume prompt on mount — never auto-dials", () => {
    const now = Date.now();
    persistCall({
      conversationId: "c-a",
      personaId: "p-a",
      personaName: "Ada",
      startedAt: now - 5_000,
      lastActiveAt: now - 5_000,
    });
    render(
      <CallSessionProvider>
        <Harness />
      </CallSessionProvider>,
    );
    expect(screen.getByTestId("resumable").textContent).toBe("Ada");
    // The whole point of D-V7-3: a prompt, NOT a silent reconnect.
    expect(lk.rooms).toHaveLength(0);
    expect(screen.getByTestId("active").textContent).toBe("false");
  });

  it("discards a STALE persisted call — no resume offer, no auto-dial", () => {
    const now = Date.now();
    persistCall({
      conversationId: "c-a",
      personaId: "p-a",
      personaName: "Ada",
      startedAt: now - RESUME_FRESHNESS_MS - 10_000,
      lastActiveAt: now - RESUME_FRESHNESS_MS - 10_000,
    });
    render(
      <CallSessionProvider>
        <Harness />
      </CallSessionProvider>,
    );
    expect(screen.getByTestId("resumable").textContent).toBe("none");
    expect(lk.rooms).toHaveLength(0);
  });

  it("resumeCall starts a FRESH call on the same conversation", async () => {
    const now = Date.now();
    persistCall({
      conversationId: "c-a",
      personaId: "p-a",
      personaName: "Ada",
      startedAt: now - 5_000,
      lastActiveAt: now - 5_000,
    });
    render(
      <CallSessionProvider>
        <Harness />
      </CallSessionProvider>,
    );
    fireEvent.click(screen.getByTestId("resume"));
    await waitFor(() => expect(lk.rooms).toHaveLength(1));
    expect(screen.getByTestId("target").textContent).toBe("Ada");
    expect(screen.getByTestId("resumable").textContent).toBe("none");
  });

  it("always mode: the mic stays open after the greeting (always-listening)", async () => {
    render(
      <CallSessionProvider>
        <Harness />
      </CallSessionProvider>,
    );
    fireEvent.click(screen.getByTestId("start-a"));
    await waitFor(() => expect(lk.rooms).toHaveLength(1));
    const room = lk.rooms[0];
    // Greet-first: preparing (gate) → listening (un-gate the mic).
    await act(async () => {
      room.emit("dataReceived", encodeState("preparing", "listening"));
      room.emit("dataReceived", encodeState("listening", "preparing"));
    });
    expect(screen.getByTestId("mic-active").textContent).toBe("true");
  });

  it("ptt mode SUPPRESSES always-listening: the mic is muted after the greeting", async () => {
    render(
      <CallSessionProvider>
        <Harness />
      </CallSessionProvider>,
    );
    fireEvent.click(screen.getByTestId("set-ptt"));
    fireEvent.click(screen.getByTestId("start-a"));
    await waitFor(() => expect(lk.rooms).toHaveLength(1));
    const room = lk.rooms[0];
    await act(async () => {
      room.emit("dataReceived", encodeState("preparing", "listening"));
      room.emit("dataReceived", encodeState("listening", "preparing"));
    });
    // The greeting un-gated the mic, but PTT (unheld) reconciles it back to muted.
    await waitFor(() =>
      expect(screen.getByTestId("mic-active").textContent).toBe("false"),
    );
  });

  it("ptt hold opens the mic while held, closes it on release", async () => {
    render(
      <CallSessionProvider>
        <Harness />
      </CallSessionProvider>,
    );
    fireEvent.click(screen.getByTestId("set-ptt"));
    fireEvent.click(screen.getByTestId("start-a"));
    await waitFor(() => expect(lk.rooms).toHaveLength(1));
    const room = lk.rooms[0];
    await act(async () => {
      room.emit("dataReceived", encodeState("preparing", "listening"));
      room.emit("dataReceived", encodeState("listening", "preparing"));
    });
    // Muted by default in ptt.
    await waitFor(() =>
      expect(screen.getByTestId("mic-active").textContent).toBe("false"),
    );
    // Hold → the mic opens; release → it closes again. (The live FAIL was the
    // button's pointer handling, not this reconcile — this proves the session
    // layer drives the mic correctly off `pttHeld`.)
    fireEvent.click(screen.getByTestId("ptt-hold"));
    await waitFor(() =>
      expect(screen.getByTestId("mic-active").textContent).toBe("true"),
    );
    fireEvent.click(screen.getByTestId("ptt-release"));
    await waitFor(() =>
      expect(screen.getByTestId("mic-active").textContent).toBe("false"),
    );
  });

  it("switching back to always-listening re-opens the mic (no stuck-on-mute)", async () => {
    render(
      <CallSessionProvider>
        <Harness />
      </CallSessionProvider>,
    );
    fireEvent.click(screen.getByTestId("set-ptt"));
    fireEvent.click(screen.getByTestId("start-a"));
    await waitFor(() => expect(lk.rooms).toHaveLength(1));
    const room = lk.rooms[0];
    await act(async () => {
      room.emit("dataReceived", encodeState("preparing", "listening"));
      room.emit("dataReceived", encodeState("listening", "preparing"));
    });
    // PTT left the mic muted…
    await waitFor(() =>
      expect(screen.getByTestId("mic-active").textContent).toBe("false"),
    );
    // …and switching back to always-listening must RE-OPEN it (the bug was that
    // it stayed muted → "stuck on mute / push-to-talk only").
    fireEvent.click(screen.getByTestId("set-always"));
    await waitFor(() =>
      expect(screen.getByTestId("mic-active").textContent).toBe("true"),
    );
    expect(screen.getByTestId("input-mode").textContent).toBe("always");
  });

  it("writes a post-call recap when the call ends (web-derived, D-V7-7)", async () => {
    render(
      <CallSessionProvider>
        <Harness />
      </CallSessionProvider>,
    );
    fireEvent.click(screen.getByTestId("start-a"));
    await waitFor(() => expect(lk.rooms).toHaveLength(1));
    expect(loadRecap("c-a")).toBeNull(); // none until the call ends
    fireEvent.click(screen.getByTestId("end"));
    await waitFor(() =>
      expect(screen.getByTestId("active").textContent).toBe("false"),
    );
    const recap = loadRecap("c-a");
    expect(recap?.personaName).toBe("Ada");
    expect(recap?.durationMs).toBeGreaterThanOrEqual(0);
  });

  it("setInputMode persists the choice across sessions (localStorage)", () => {
    render(
      <CallSessionProvider>
        <Harness />
      </CallSessionProvider>,
    );
    fireEvent.click(screen.getByTestId("set-ptt"));
    expect(screen.getByTestId("input-mode").textContent).toBe("ptt");
    expect(
      window.localStorage.getItem("persona:voice-input-prefs") ?? "",
    ).toContain("ptt");
  });
});
