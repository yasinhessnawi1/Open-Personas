/**
 * R9-053 — regression coverage for the two client robustness gaps a server-side
 * `delete_room` exposed: (1) the mic-sync effect must never call
 * `setMicrophoneEnabled` against a Room that isn't `Connected` (that's what
 * produced livekit-client's "could not createOffer with closed peer
 * connection" warning), and (2) a non-client-initiated disconnect must release
 * `roomRef` so the Retry button's `start()` isn't a silent no-op against a
 * stale Room.
 *
 * Mirrors `call-session-context.test.tsx`'s fake-Room mock (a settable
 * `.state` is the one addition — the shared mock there never varies it).
 */

import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useVoiceCall } from "./use-voice-call";

const lk = vi.hoisted(() => {
  const rooms: FakeRoom[] = [];
  class FakeRoom {
    handlers = new Map<string, (...a: unknown[]) => void>();
    canPlaybackAudio = true;
    state = "connected";
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
  return { rooms, FakeRoom };
});

vi.mock("livekit-client", () => ({
  Room: lk.FakeRoom,
  RemoteAudioTrack: class {},
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

const tokenMock = vi.hoisted(() => ({
  fn: vi.fn(async () => ({ token: "tok", livekitUrl: "ws://lk" })),
}));
vi.mock("./token", () => ({ fetchVoiceToken: tokenMock.fn }));

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

// The two `dataReceived` frames that finish the greet-first ring and un-gate
// the mic (`applyVoiceStateEvent` in call-state.ts) — the trigger that lands
// the mic-sync effect on a real `state.micActive` change.
function unGateMic(room: InstanceType<typeof lk.FakeRoom>): void {
  room.emit("dataReceived", encodeState("preparing", "listening"));
  room.emit("dataReceived", encodeState("listening", "preparing"));
}

beforeEach(() => {
  lk.rooms.length = 0;
  tokenMock.fn.mockClear();
  tokenMock.fn.mockImplementation(async () => ({
    token: "tok",
    livekitUrl: "ws://lk",
  }));
});
afterEach(() => {
  for (const el of document.querySelectorAll("audio")) el.remove();
});

describe("useVoiceCall — mic-sync Connected guard (R9-053)", () => {
  it("does not publish the mic onto a Room that is not Connected", async () => {
    const { result } = renderHook(() =>
      useVoiceCall({
        personaId: "p-a",
        conversationId: "c-a",
        getToken: async () => "jwt",
      }),
    );
    await act(async () => {
      await result.current.start();
    });
    const room = lk.rooms[0];

    // Simulate the server tearing the room down right as the un-gate lands —
    // the Room reports non-Connected before the effect re-fires.
    room.state = "reconnecting";
    room.localParticipant.setMicrophoneEnabled.mockClear();

    await act(async () => {
      unGateMic(room);
    });

    expect(room.localParticipant.setMicrophoneEnabled).not.toHaveBeenCalled();
  });

  it("publishes the mic once the Room reports Connected", async () => {
    const { result } = renderHook(() =>
      useVoiceCall({
        personaId: "p-a",
        conversationId: "c-a",
        getToken: async () => "jwt",
      }),
    );
    await act(async () => {
      await result.current.start();
    });
    const room = lk.rooms[0];
    room.state = "connected";
    room.localParticipant.setMicrophoneEnabled.mockClear();

    await act(async () => {
      unGateMic(room);
    });

    expect(room.localParticipant.setMicrophoneEnabled).toHaveBeenCalledWith(
      true,
    );
  });
});

describe("useVoiceCall — Retry after a server-side drop (R9-053)", () => {
  it("releases the stale Room on a non-client disconnect so a subsequent start() is not a no-op", async () => {
    const { result } = renderHook(() =>
      useVoiceCall({
        personaId: "p-a",
        conversationId: "c-a",
        getToken: async () => "jwt",
      }),
    );
    await act(async () => {
      await result.current.start();
    });
    expect(lk.rooms).toHaveLength(1);

    // A server-side delete_room surfaces as a non-CLIENT_INITIATED disconnect.
    // Force the hook's one reconnect attempt to also fail, so it falls through
    // to "dropped" (mirrors the real 600s-TTL-expired case).
    tokenMock.fn.mockRejectedValueOnce(new Error("token refetch failed"));
    act(() => {
      lk.rooms[0].emit("disconnected", "SERVER_SHUTDOWN");
    });
    await waitFor(() => expect(result.current.state.phase).toBe("dropped"));

    // Before the fix, `roomRef` stayed pointed at the torn-down Room, so
    // `start()`'s reentry guard (`if (roomRef.current || startingRef.current)
    // return;`) silently no-op'd here — the Retry button was dead.
    await act(async () => {
      await result.current.start();
    });
    await waitFor(() => expect(lk.rooms).toHaveLength(2));
    expect(lk.rooms[1].connect).toHaveBeenCalledTimes(1);
  });

  it("releases the Room on a clean, client-initiated end so a subsequent start() works too", async () => {
    const { result } = renderHook(() =>
      useVoiceCall({
        personaId: "p-a",
        conversationId: "c-a",
        getToken: async () => "jwt",
      }),
    );
    await act(async () => {
      await result.current.start();
    });
    expect(lk.rooms).toHaveLength(1);

    act(() => {
      lk.rooms[0].emit("disconnected", "CLIENT_INITIATED");
    });
    await waitFor(() => expect(result.current.state.phase).toBe("ended"));

    await act(async () => {
      await result.current.start();
    });
    await waitFor(() => expect(lk.rooms).toHaveLength(2));
    expect(lk.rooms[1].connect).toHaveBeenCalledTimes(1);
  });
});
