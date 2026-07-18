"use client";

/**
 * Spec V6 A3 — the WebRTC voice-call client hook (the client half of V1).
 *
 * Owns one LiveKit Room per call: token fetch → connect → publish mic → play the
 * persona's audio → decode the data-channel (state + captions) → clean teardown.
 * The connection-state machine + autoplay + mic-permission handling (D-V6-5) and
 * the E3 token refresh (re-fetch + reconnect on a hard drop, so a call outlives
 * the 600s token TTL) all live here.
 *
 * **Audio levels are NOT React state.** The user mic level (D-V6-6 "I'm hearing
 * you") and the persona TTS level (D-V6-1 speaking morph) update at 60fps, so
 * they are exposed as pure getters the orb polls in its OWN rAF — pushing them
 * through `setState` would flood re-renders. React state carries only what
 * changes coarsely: phase, agentState, barge-in signal, mic-active, autoplay.
 *
 * Pure mapping logic lives in `call-state.ts` (unit-tested); this module is the
 * SDK-bound glue, exercised live by the Playwright operator pass (criterion 12).
 */

import {
  ConnectionState,
  createAudioAnalyser,
  DisconnectReason,
  RemoteAudioTrack,
  type RemoteTrack,
  Room,
  RoomEvent,
  Track,
} from "livekit-client";
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, type TokenGetter } from "@/lib/api/client";
import {
  applyActivity,
  applyVoiceStateEvent,
  type CallPhase,
  callErrorForMediaError,
  callErrorForTokenStatus,
  callPhaseForConnectionState,
  INITIAL_CALL_STATE,
  mergeArtifacts,
  type VoiceActivity,
  type VoiceCallState,
} from "./call-state";
import { type CaptionSegment, upsertCaption } from "./captions";
import { fetchVoiceToken } from "./token";
import { parseVoiceEvent, type VoiceArtifact } from "./voice-events";

export interface UseVoiceCallOptions {
  personaId: string;
  conversationId: string;
  getToken: TokenGetter;
}

export interface VoiceCall {
  state: VoiceCallState;
  /** Live caption segments (user ASR + persona verbatim) for the D-V6-2 surface. */
  captions: CaptionSegment[];
  /** Rich-output artifacts produced during the call, deduped by `workspacePath`
   * (V10-D-6) — the call surface mounts the latest in the FileRendererPanel. */
  artifacts: VoiceArtifact[];
  /** Capabilities the persona is currently using (V10-D-6) — drives the live
   * "using <X>…" badge; cleared as each `activity_end` arrives. */
  activities: VoiceActivity[];
  /** Start the call (must be called from a user gesture so audio autoplay unlocks). */
  start: () => Promise<void>;
  /** End the call cleanly and release the mic. */
  end: () => Promise<void>;
  /** Toggle the mic mute. */
  toggleMute: () => Promise<void>;
  /** Unlock audio playback after an autoplay block (call from a user gesture). */
  enableAudio: () => Promise<void>;
  /** Current user mic level 0..1 — the orb polls this (D-V6-6). */
  getMicLevel: () => number;
  /** Current persona TTS level 0..1 — the orb polls this (D-V6-1 speaking). */
  getPersonaLevel: () => number;
}

interface Analyser {
  calculateVolume: () => number;
  cleanup: () => void;
}

interface MintedToken {
  livekitUrl: string;
  token: string;
}

// Client-side ring backstop (Spec 32 D-32-3) — slightly past the agent's 30s
// greet watchdog, so the agent's own degrade (→ a LISTENING frame that un-gates
// us) normally fires first; this only catches the agent never joining at all.
const RING_BACKSTOP_MS = 35_000;

export function useVoiceCall(options: UseVoiceCallOptions): VoiceCall {
  const [state, setState] = useState<VoiceCallState>(INITIAL_CALL_STATE);
  const [captions, setCaptions] = useState<CaptionSegment[]>([]);
  const [artifacts, setArtifacts] = useState<VoiceArtifact[]>([]);
  const [activities, setActivities] = useState<VoiceActivity[]>([]);

  const roomRef = useRef<Room | null>(null);
  const audioElsRef = useRef<HTMLMediaElement[]>([]);
  const micAnalyserRef = useRef<Analyser | null>(null);
  const personaAnalyserRef = useRef<Analyser | null>(null);
  const endedByUserRef = useRef(false);
  const reconnectTriedRef = useRef(false);
  // Synchronous in-flight guard: `roomRef` isn't set until AFTER the async token
  // fetch, so two rapid `start()` calls (the auto-start effect under React Strict
  // Mode, a double-render, or a double-click) would both pass a `roomRef`-only
  // check and each launch its own agent + Room — the user joins both and hears
  // the turn-0 greeting twice. This flag is set before the first await.
  const startingRef = useRef(false);
  // Keep the latest options accessible inside long-lived SDK callbacks without
  // re-subscribing every render.
  const optionsRef = useRef(options);
  optionsRef.current = options;

  const patch = useCallback((p: Partial<VoiceCallState>) => {
    setState((s) => ({ ...s, ...p }));
  }, []);

  const getMicLevel = useCallback(
    () => micAnalyserRef.current?.calculateVolume() ?? 0,
    [],
  );
  const getPersonaLevel = useCallback(
    () => personaAnalyserRef.current?.calculateVolume() ?? 0,
    [],
  );

  const teardownAudio = useCallback(() => {
    micAnalyserRef.current?.cleanup();
    micAnalyserRef.current = null;
    personaAnalyserRef.current?.cleanup();
    personaAnalyserRef.current = null;
    for (const el of audioElsRef.current) {
      el.pause();
      el.srcObject = null;
      el.remove();
    }
    audioElsRef.current = [];
  }, []);

  const handleData = useCallback((payload: Uint8Array) => {
    const event = parseVoiceEvent(payload);
    if (event === null) return;
    if (event.type === "transcript") {
      // C1 — accumulate the caption segment (mutate-and-replace by id). The
      // dual-region ARIA split (visual vs SR) is the renderer's concern.
      setCaptions((c) => upsertCaption(c, event));
      return;
    }
    // V10-D-6 rich-output frames. Routed BEFORE the state reducer because
    // `applyVoiceStateEvent` is typed for `VoiceStateEvent` only — these branches
    // narrow the union away so only `state` events reach it. The panel mounts the
    // artifacts; the badge reads the activities; captions stay separate (audio is
    // the persona's narration — never re-rendered here as a duplicate artifact).
    if (event.type === "tool_result") {
      setArtifacts((a) => mergeArtifacts(a, event));
      return;
    }
    if (event.type === "activity_start" || event.type === "activity_end") {
      setActivities((a) => applyActivity(a, event));
      return;
    }
    // Spec 32 C — the ring lifecycle (preparing → greeting → un-gate), barge-in,
    // and orb cue are all folded in the pure reducer; the mic-sync effect mirrors
    // the resulting `micActive` onto the LiveKit track.
    if (event.type === "state") {
      setState((s) => applyVoiceStateEvent(s, event));
    }
  }, []);

  const fetchToken = useCallback(
    (): Promise<MintedToken> =>
      fetchVoiceToken({
        personaId: optionsRef.current.personaId,
        conversationId: optionsRef.current.conversationId,
        getToken: optionsRef.current.getToken,
      }),
    [],
  );

  const connectMicAndAudio = useCallback(
    async (room: Room, token: MintedToken) => {
      await room.connect(token.livekitUrl, token.token);
      // Unlock autoplay inside the same gesture that started the call.
      await room.startAudio().catch(() => undefined);
      // A Disconnected can race in during connect()/startAudio() (e.g. an
      // immediate server-side room teardown) — bail before patching a phase
      // that implies a live connection, and before the mic-sync effect gets
      // any excuse to touch a dead Room (R9-053).
      if (room.state !== ConnectionState.Connected) return;
      // Spec 32 C3 — greet-first: the persona answers first, so the call opens
      // RINGING with the mic gated. The mic is NOT enabled here; the mic-sync
      // effect publishes it the moment the greeting finishes (un-gate at
      // completion), so the greeting and the user's first words never collide.
      patch({
        phase: "ringing",
        micGatedForGreeting: true,
        micActive: false,
        needsAudioGesture: !room.canPlaybackAudio,
      });
    },
    [patch],
  );

  // Mirror `micActive` onto the LiveKit mic track — the single mic-control point
  // (Spec 32 C3). The greeting un-gate (reducer) and the mute toggle both just
  // flip `micActive`; this effect publishes/unpublishes + (re)builds the level
  // analyser. Gated during the ring, live from greeting-end onward.
  useEffect(() => {
    const room = roomRef.current;
    if (!room || roomRef.current === null) return;
    // Guard against publishing onto a closing/closed peer connection — a
    // server-side teardown (e.g. the RING_BACKSTOP_MS timer landing right as
    // the room is deleted) can leave the Room non-Connected while this effect
    // is still queued; renegotiating against a closed PC is what produced the
    // livekit-client "could not createOffer with closed peer connection"
    // warning (R9-053).
    if (room.state !== ConnectionState.Connected) return;
    let cancelled = false;
    void room.localParticipant
      .setMicrophoneEnabled(state.micActive)
      .then(() => {
        if (cancelled) return;
        if (!state.micActive) {
          micAnalyserRef.current?.cleanup();
          micAnalyserRef.current = null;
          return;
        }
        const micTrack = room.localParticipant.getTrackPublication(
          Track.Source.Microphone,
        )?.audioTrack;
        if (micTrack && micAnalyserRef.current === null) {
          // cloneTrack lets the level read even while the published track is muted.
          micAnalyserRef.current = createAudioAnalyser(micTrack, {
            cloneTrack: true,
          });
        }
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [state.micActive]);

  // Never ring forever (Spec 32 D-32-3, client half). The agent's own greet
  // watchdog degrades to LISTENING (which un-gates us); this is the backstop for
  // the agent never joining at all — after it, open the floor to the user.
  useEffect(() => {
    if (state.phase !== "ringing") return;
    const timer = setTimeout(() => {
      setState((s) =>
        s.phase === "ringing"
          ? {
              ...s,
              phase: "connected",
              micActive: true,
              micGatedForGreeting: false,
            }
          : s,
      );
    }, RING_BACKSTOP_MS);
    return () => clearTimeout(timer);
  }, [state.phase]);

  const handleDisconnect = useCallback(
    async (room: Room, reason?: DisconnectReason) => {
      const clientInitiated =
        endedByUserRef.current || reason === DisconnectReason.CLIENT_INITIATED;
      if (clientInitiated) {
        teardownAudio();
        patch({ phase: "ended", micActive: false });
        // Release the torn-down Room — mirrors `end()` — so a stale instance
        // can't be touched by another effect and so `start()`'s reentry guard
        // doesn't wrongly treat a finished call as still in flight (R9-053).
        roomRef.current = null;
        return;
      }
      // E3 — a hard drop: the SDK's own resume reuses the cached token (fine for
      // a brief blip), but a reconnect AFTER the 600s TTL needs a FRESH token.
      // Try exactly one re-fetch + reconnect; on failure, surface "dropped".
      if (!reconnectTriedRef.current) {
        reconnectTriedRef.current = true;
        patch({ phase: "reconnecting" });
        try {
          const token = await fetchToken();
          await connectMicAndAudio(room, token);
          return; // the Connected event restores the phase
        } catch {
          // fall through to dropped
        }
      }
      teardownAudio();
      patch({ phase: "dropped", micActive: false });
      // Same release as the client-initiated path above: a non-client
      // disconnect (e.g. a server-side delete_room) must not leave a stale
      // Room in `roomRef`, or the Retry button on the "dropped" terminal card
      // is dead — `start()`'s reentry guard would silently no-op (R9-053).
      roomRef.current = null;
    },
    [connectMicAndAudio, fetchToken, patch, teardownAudio],
  );

  const wireRoom = useCallback(
    (room: Room) => {
      room.on(RoomEvent.ConnectionStateChanged, (cs: ConnectionState) => {
        if (cs === ConnectionState.Disconnected) return; // owned by Disconnected
        const mapped = callPhaseForConnectionState(cs);
        setState((s) => {
          // Greet-first (Spec 32 C): a "connected" connection event must NOT end
          // the ring — the greeting frames drive ringing → connected. Other
          // phases (reconnecting/…) still apply.
          if (mapped === "connected" && s.micGatedForGreeting) return s;
          return { ...s, phase: mapped };
        });
      });
      room.on(RoomEvent.TrackSubscribed, (track: RemoteTrack) => {
        if (!(track instanceof RemoteAudioTrack)) return;
        const el = track.attach();
        el.style.display = "none";
        document.body.appendChild(el);
        audioElsRef.current.push(el);
        personaAnalyserRef.current?.cleanup();
        personaAnalyserRef.current = createAudioAnalyser(track, {});
      });
      room.on(RoomEvent.DataReceived, (payload: Uint8Array) =>
        handleData(payload),
      );
      room.on(RoomEvent.AudioPlaybackStatusChanged, () => {
        patch({ needsAudioGesture: !room.canPlaybackAudio });
      });
      room.on(RoomEvent.Disconnected, (reason?: DisconnectReason) => {
        void handleDisconnect(room, reason);
      });
    },
    [handleData, handleDisconnect, patch],
  );

  const start = useCallback(async () => {
    // Guard against a room already up AND a start already in flight (the async
    // token fetch below means a roomRef-only check races — see startingRef).
    if (roomRef.current || startingRef.current) return;
    startingRef.current = true;
    try {
      endedByUserRef.current = false;
      reconnectTriedRef.current = false;
      setCaptions([]);
      setArtifacts([]);
      setActivities([]);
      patch({ phase: "connecting", error: null });

      let token: MintedToken;
      try {
        token = await fetchToken();
      } catch (err) {
        const error =
          err instanceof ApiError
            ? callErrorForTokenStatus(err.status)
            : {
                kind: "service_unavailable" as const,
                message: "The voice service is unavailable.",
              };
        patch({ phase: "error", error });
        return;
      }

      const room = new Room({
        audioCaptureDefaults: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });
      roomRef.current = room;
      wireRoom(room);

      try {
        await connectMicAndAudio(room, token);
      } catch (err) {
        patch({ phase: "error", error: callErrorForMediaError(err) });
        endedByUserRef.current = true;
        await room.disconnect().catch(() => undefined);
        teardownAudio();
        roomRef.current = null;
      }
    } finally {
      startingRef.current = false;
    }
  }, [connectMicAndAudio, fetchToken, patch, teardownAudio, wireRoom]);

  const end = useCallback(async () => {
    const room = roomRef.current;
    if (!room) return;
    endedByUserRef.current = true;
    await room.disconnect().catch(() => undefined);
    teardownAudio();
    roomRef.current = null;
  }, [teardownAudio]);

  const toggleMute = useCallback(async () => {
    // Flip `micActive`; the mic-sync effect performs the LiveKit call (single
    // mic-control point, Spec 32 C3). A no-op while the mic is gated for the
    // greeting — the user can't unmute before the persona has finished greeting.
    setState((s) =>
      s.micGatedForGreeting ? s : { ...s, micActive: !s.micActive },
    );
  }, []);

  const enableAudio = useCallback(async () => {
    const room = roomRef.current;
    if (!room) return;
    await room.startAudio().catch(() => undefined);
    patch({ needsAudioGesture: !room.canPlaybackAudio });
  }, [patch]);

  // Teardown on unmount — a call must never outlive its surface.
  useEffect(() => {
    return () => {
      const room = roomRef.current;
      if (room) {
        endedByUserRef.current = true;
        void room.disconnect().catch(() => undefined);
      }
      teardownAudio();
      roomRef.current = null;
    };
  }, [teardownAudio]);

  return {
    state,
    captions,
    artifacts,
    activities,
    start,
    end,
    toggleMute,
    enableAudio,
    getMicLevel,
    getPersonaLevel,
  };
}

export type { CallPhase };
