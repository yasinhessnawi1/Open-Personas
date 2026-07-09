"use client";

/**
 * Spec V7 D-V7-1 — the app-level call-session provider (the hoist).
 *
 * The whole continuity mechanism: this provider wraps the ONE {@link useVoiceCall}
 * instance and is mounted ONCE in `AppShell` (the "mount-once-in-the-shell"
 * precedent — `ToastProvider` / `CommandPalette`), ABOVE the routed pages. Because
 * the App Router keeps the `(app)` layout mounted across client-side navigation
 * (proven empirically — the GATE-ZERO linchpin probe), the call's LiveKit `Room`,
 * its `<audio>` sinks, and mic publication live here and are NOT torn down when the
 * user navigates between pages. The full call view (T3) and the mini-bar (T2) BIND
 * to this session; they never own a `Room`.
 *
 * **HARD GUARD (D-V7-1, non-negotiable):** the `Room` + `<audio>` + mic live in
 * THIS provider (the `useVoiceCall` instance below), never inside a route/page.
 * `useVoiceCall` is the single point of `Room` ownership in the whole app — so the
 * call survives navigation and the invariant holds even if Cache Components is
 * enabled later (the provider sits in the always-mounted layout, never a hideable
 * route). `useVoiceCall`'s internals are consumed UNCHANGED (its connection-state
 * machine, autoplay/mic handling, the E3 token refresh, and the audio-levels-as-
 * getters design all stay exactly as V6 shipped them).
 *
 * **Single-active-call invariant:** exactly one call at a time. `useVoiceCall`'s
 * own `startingRef` + `roomRef` guards (a second `start()` no-ops while one is in
 * flight or up) are inherited verbatim; `startCall` additionally ignores a request
 * while a call is already active (the switch/replace flow is T4/D-V7-4). The
 * provider adds no `Room` of its own, so there is nothing to double-mount: a React
 * Strict-Mode mount→unmount→mount of the provider creates zero `Room`s (no call is
 * active at app load), and an active call's `Room` is disconnected by
 * `useVoiceCall`'s unmount cleanup if the provider ever truly unmounts.
 *
 * The shell stays a server component: this is a client provider that takes
 * `children` (the server-rendered routes pass through as a slot), so a call-state
 * change re-renders only this client subtree — it never re-runs `AppShell`'s
 * server-side `fetchSidebarData`.
 */

import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useAuth } from "@/auth";
import { useSidebarRefresh } from "@/lib/hooks/use-sidebar-refresh";
import {
  clearPersistedCall,
  isResumable,
  loadPersistedCall,
  type PersistedCall,
  persistCall,
} from "@/lib/voice/call-persistence";
import { clearRecap, saveRecap } from "@/lib/voice/call-recap";
import type { VoiceActivity, VoiceCallState } from "@/lib/voice/call-state";
import type { CaptionSegment } from "@/lib/voice/captions";
import {
  type InputMode,
  loadInputPrefs,
  saveInputPrefs,
} from "@/lib/voice/input-prefs";
import { useVoiceCall } from "@/lib/voice/use-voice-call";
import type { VoiceArtifact } from "@/lib/voice/voice-events";

/** How often we refresh the persisted call's freshness anchor while live. */
const HEARTBEAT_MS = 15_000;

const TEMPLATE = process.env.NEXT_PUBLIC_CLERK_JWT_TEMPLATE;

/**
 * The persona + conversation a call is placed against, plus the display fields
 * the mini-bar / indicators need to render the call from anywhere (T2/T5/T6) —
 * so a consumer never has to re-fetch the persona to show "on a call with X".
 */
export interface CallTarget {
  readonly personaId: string;
  readonly conversationId: string;
  readonly personaName: string;
  readonly personaAvatarUrl?: string | null;
  readonly personaRole?: string;
}

/** The shared call session every surface binds to (full view, mini-bar, indicators). */
export interface CallSession {
  /** The live connection-state machine (phase, agentState, barge-in, mic, autoplay). */
  readonly state: VoiceCallState;
  /** Live caption segments (user ASR + persona verbatim). */
  readonly captions: CaptionSegment[];
  /** Rich-output artifacts produced during the call (V10-D-6) — the full surface
   * mounts the latest in the FileRendererPanel. Empty when no call is active. */
  readonly artifacts: VoiceArtifact[];
  /** Capabilities the persona is currently using (V10-D-6) — drives the live
   * "using <X>…" badge. Empty when no call is active. */
  readonly activities: VoiceActivity[];
  /** Who the call is with, or `null` when no call is active. */
  readonly target: CallTarget | null;
  /** True while a call is placed (target set and not yet cleared). */
  readonly isActive: boolean;
  /** Epoch ms when the active call was started, or `null` when idle (the mini-bar
   * timer reads this; T5 persists it to `sessionStorage` for resume-after-reload). */
  readonly startedAt: number | null;
  /** A call the user asked to start while one is already active — drives the
   * end-and-switch confirm (D-V7-4). `null` when there's nothing pending. */
  readonly pendingSwitch: CallTarget | null;
  /** Begin a call against `target`. No-ops while a call is already active. */
  start: (target: CallTarget) => void;
  /**
   * The entry-point intent: ask to call `target`. Returns the outcome so the
   * caller can decide whether to navigate now or wait for the switch confirm:
   * - `"started"` — no call was active; the call is starting (navigate now).
   * - `"current"` — a call for this same conversation is already active (navigate
   *   to its full view; nothing to start).
   * - `"switch"` — a *different* call is active; a confirm is now pending (do NOT
   *   navigate — {@link confirmSwitch} navigates on confirm).
   */
  requestCall: (target: CallTarget) => "started" | "current" | "switch";
  /** Confirm the pending end-and-switch: ends the active call, THEN starts the
   * pending one (serialized — never two Rooms at once). Resolves once the old
   * call is torn down and the new one requested. */
  confirmSwitch: () => Promise<void>;
  /** Dismiss the pending end-and-switch, keeping the current call. */
  cancelSwitch: () => void;
  /** A recent call found in `sessionStorage` on load — the resume-after-reload
   * candidate (D-V7-3). `null` unless a fresh prior call is offered to resume.
   * Surfacing it is a PROMPT; resuming is always an explicit user action. */
  readonly resumable: PersistedCall | null;
  /** Resume the offered call — starts a FRESH call on the same conversation (a
   * reconnect, not a preserved connection). Call from a user gesture. */
  resumeCall: () => void;
  /** Dismiss the resume offer and forget the persisted call. */
  dismissResume: () => void;
  /** End the active call cleanly and release the mic. */
  end: () => Promise<void>;
  /** Toggle the mic mute. */
  toggleMute: () => Promise<void>;
  /** The input mode (D-V7-6): `always`-listening or push-to-`ptt`. Persisted. */
  readonly inputMode: InputMode;
  /** Switch the input mode (persisted). In `ptt` the mic is open only while held. */
  setInputMode: (mode: InputMode) => void;
  /** Whether the push-to-talk control is currently held (mic open in `ptt` mode). */
  readonly pttHeld: boolean;
  /** Set the hold state — the hold-to-talk button (press/release) drives this. */
  setPttHeld: (held: boolean) => void;
  /** Unlock audio playback after an autoplay block (call from a user gesture). */
  enableAudio: () => Promise<void>;
  /** Current user mic level 0..1 — the orb polls this. */
  getMicLevel: () => number;
  /** Current persona TTS level 0..1 — the orb polls this. */
  getPersonaLevel: () => number;
}

const CallSessionContext = createContext<CallSession | null>(null);

export function CallSessionProvider({
  children,
}: {
  children: ReactNode;
}): React.JSX.Element {
  const { getToken } = useAuth();
  const refreshSidebar = useSidebarRefresh();
  const token = useCallback(
    () => getToken(TEMPLATE ? { template: TEMPLATE } : undefined),
    [getToken],
  );

  // The active target drives `useVoiceCall`'s options. `null` (empty ids) leaves
  // the hook inert — no token fetch, no `Room`, no mic — until `start` is called.
  const [target, setTarget] = useState<CallTarget | null>(null);
  const [startedAt, setStartedAt] = useState<number | null>(null);
  const [pendingSwitch, setPendingSwitch] = useState<CallTarget | null>(null);
  const [resumable, setResumable] = useState<PersistedCall | null>(null);
  // Input mode + PTT hold (D-V7-6). Initialised to the default so SSR + the first
  // client render agree (no hydration mismatch); the saved pref loads in an effect.
  const [inputMode, setInputModeState] = useState<InputMode>("always");
  const [pttKey, setPttKey] = useState("Space");
  const [pttHeld, setPttHeld] = useState(false);
  // A start is requested but must wait until `target` has propagated into the
  // hook's options: `useVoiceCall` captures ids into `optionsRef` during render,
  // so `start()` must run in an effect AFTER the re-render, not inline in
  // `start(target)` (which would mint a token for the stale/empty ids).
  const pendingStartRef = useRef(false);
  // The active call's recap inputs, captured at start so the post-call recap can
  // be written when the call ends (the session state is cleared by then). A ref,
  // not state — it's read once on end, never rendered.
  const recapRef = useRef<{
    conversationId: string;
    personaName: string;
    startedAt: number;
  } | null>(null);

  const call = useVoiceCall({
    personaId: target?.personaId ?? "",
    conversationId: target?.conversationId ?? "",
    getToken: token,
  });
  const {
    start: startInner,
    end: endInner,
    toggleMute,
    enableAudio,
    getMicLevel,
    getPersonaLevel,
  } = call;

  // Fire the pending start once the new target is live in the hook's options.
  useEffect(() => {
    if (target !== null && pendingStartRef.current) {
      pendingStartRef.current = false;
      void startInner();
    }
  }, [target, startInner]);

  // Write the post-call recap from the captured lifecycle inputs (D-V7-7) — once,
  // before the session state is cleared. Web-derived; no server write.
  const flushRecap = useCallback(() => {
    const c = recapRef.current;
    if (c === null) return;
    recapRef.current = null;
    const endedAt = Date.now();
    saveRecap({
      conversationId: c.conversationId,
      personaName: c.personaName,
      durationMs: endedAt - c.startedAt,
      endedAt,
    });
  }, []);

  // A clean hang-up clears the session so the mini-bar / indicators drop away.
  // Terminal-but-recoverable phases (`dropped` / `error`) KEEP the target so the
  // surface can offer retry (D-V7-3 / the V6 retry affordance).
  useEffect(() => {
    if (call.state.phase === "ended") {
      flushRecap();
      setTarget(null);
      setStartedAt(null);
      clearPersistedCall();
    }
  }, [call.state.phase, flushRecap]);

  // On load (after a reload / hard nav), surface a fresh prior call as a resume
  // PROMPT — never auto-dial (D-V7-3). Stale entries are discarded. Runs once per
  // provider lifetime; on a fresh load no call is active, so this can't fight a
  // live session.
  useEffect(() => {
    const record = loadPersistedCall();
    if (record === null) return;
    if (isResumable(record, Date.now())) setResumable(record);
    else clearPersistedCall();
  }, []);

  // Heartbeat the freshness anchor while a call is live, plus a `pagehide` write
  // for an exact stamp on reload/close. Anchoring on `lastActiveAt` (not
  // `startedAt`) keeps a long call resumable after a reload; the interval covers
  // a client-side escape (provider unmount) that `pagehide` doesn't fire on.
  const conversationId = target?.conversationId ?? null;
  useEffect(() => {
    if (conversationId === null) return;
    const touch = () => {
      const rec = loadPersistedCall();
      if (rec !== null) persistCall({ ...rec, lastActiveAt: Date.now() });
    };
    const id = setInterval(touch, HEARTBEAT_MS);
    window.addEventListener("pagehide", touch);
    return () => {
      clearInterval(id);
      window.removeEventListener("pagehide", touch);
    };
  }, [conversationId]);

  // Load the persisted input prefs on the client (after hydration) so SSR and the
  // first render agree on the default.
  useEffect(() => {
    const prefs = loadInputPrefs();
    setInputModeState(prefs.mode);
    setPttKey(prefs.pttKey);
  }, []);

  const live =
    call.state.phase === "connected" || call.state.phase === "reconnecting";

  const setInputMode = useCallback(
    (mode: InputMode) => {
      setInputModeState(mode);
      setPttHeld(false);
      saveInputPrefs({ mode, pttKey });
      // Returning to always-listening must RE-OPEN the mic: PTT had it muted, and
      // the PTT reconcile below only manages the mic in `ptt` mode — so nothing
      // else un-mutes it. Without this, "always-listening" would stay silently
      // muted (the "stuck on mute / push-to-talk only" bug). Only while a call is
      // live and past the greeting; otherwise the greeting un-gate opens it.
      if (
        mode === "always" &&
        live &&
        !call.state.micGatedForGreeting &&
        !call.state.micActive
      ) {
        void toggleMute();
      }
    },
    [
      pttKey,
      live,
      call.state.micGatedForGreeting,
      call.state.micActive,
      toggleMute,
    ],
  );

  // PTT (D-V7-6): in push-to-talk the mic is open ONLY while held. Reconcile the
  // published mic to `pttHeld` whenever it diverges — THIS is what "suppresses
  // always-listening": after the greeting un-gates the mic (micActive → true), an
  // unheld PTT immediately mutes it again. Uses the session's own `toggleMute`;
  // `useVoiceCall` is untouched (no mic-device reach-in).
  useEffect(() => {
    if (inputMode !== "ptt" || !live) return;
    if (call.state.micGatedForGreeting) return; // can't talk during the greeting
    if (call.state.micActive !== pttHeld) void toggleMute();
  }, [
    inputMode,
    live,
    pttHeld,
    call.state.micActive,
    call.state.micGatedForGreeting,
    toggleMute,
  ]);

  // Hold-to-talk key (desktop enhancement) — only in ptt mode during a live call,
  // and ignored while typing so the key (default Space) never toggles the mic from
  // the chat composer.
  useEffect(() => {
    if (inputMode !== "ptt" || !live) return;
    const isTyping = (): boolean => {
      const el = document.activeElement;
      return (
        el instanceof HTMLElement &&
        (el.isContentEditable ||
          el.tagName === "INPUT" ||
          el.tagName === "TEXTAREA")
      );
    };
    const down = (e: KeyboardEvent) => {
      if (e.code !== pttKey || e.repeat || isTyping()) return;
      e.preventDefault();
      setPttHeld(true);
    };
    const up = (e: KeyboardEvent) => {
      if (e.code === pttKey) setPttHeld(false);
    };
    window.addEventListener("keydown", down);
    window.addEventListener("keyup", up);
    return () => {
      window.removeEventListener("keydown", down);
      window.removeEventListener("keyup", up);
    };
  }, [inputMode, live, pttKey]);

  const start = useCallback((next: CallTarget) => {
    // Single active call: a start while one is live is ignored here (the explicit
    // end-and-switch flow is T4). `useVoiceCall.start()` also guards via `roomRef`.
    pendingStartRef.current = true;
    const at = Date.now();
    setStartedAt(at);
    setTarget(next);
    setResumable(null);
    // Capture the recap inputs; clear any prior recap for this conversation so a
    // new call supersedes the old "call ended" trace.
    recapRef.current = {
      conversationId: next.conversationId,
      personaName: next.personaName,
      startedAt: at,
    };
    clearRecap(next.conversationId);
    // Persist the minimal resume record (never the token/room — dead after reload).
    persistCall({
      conversationId: next.conversationId,
      personaId: next.personaId,
      personaName: next.personaName,
      personaAvatarUrl: next.personaAvatarUrl ?? undefined,
      personaRole: next.personaRole,
      startedAt: at,
      lastActiveAt: at,
    });
  }, []);

  const end = useCallback(async () => {
    // Record the recap before tearing down (the disconnect's `ended` event may
    // not fire synchronously; writing here makes a user-initiated end reliable).
    flushRecap();
    await endInner();
    setTarget(null);
    setStartedAt(null);
    clearPersistedCall();
    // R9-012: the shared sidebar-refresh seam — the ended call moves the Calls
    // badge + the /calls history (soft refresh; client state preserved). `end`
    // is the single teardown seam, so the switch flow is covered too.
    refreshSidebar();
  }, [endInner, flushRecap, refreshSidebar]);

  const resumeCall = useCallback(() => {
    if (resumable === null) return;
    // A fresh call on the same conversation_id (reconnect, not preserved). `start`
    // sets a new persisted record and clears the resume offer.
    start({
      personaId: resumable.personaId,
      conversationId: resumable.conversationId,
      personaName: resumable.personaName,
      personaAvatarUrl: resumable.personaAvatarUrl,
      personaRole: resumable.personaRole,
    });
  }, [resumable, start]);

  const dismissResume = useCallback(() => {
    clearPersistedCall();
    setResumable(null);
  }, []);

  const requestCall = useCallback(
    (next: CallTarget): "started" | "current" | "switch" => {
      if (target === null) {
        start(next);
        return "started";
      }
      if (target.conversationId === next.conversationId) return "current";
      setPendingSwitch(next);
      return "switch";
    },
    [target, start],
  );

  const confirmSwitch = useCallback(async () => {
    if (pendingSwitch === null) return;
    const next = pendingSwitch;
    setPendingSwitch(null);
    // Serialize end → start (D-V7-4): the active Room is fully disconnected
    // (awaited) and `roomRef` nulled by `useVoiceCall.end` BEFORE `start` creates
    // the next Room — so there are never two Rooms / two mic publications at once.
    await end();
    start(next);
  }, [pendingSwitch, end, start]);

  const cancelSwitch = useCallback(() => setPendingSwitch(null), []);

  const value = useMemo<CallSession>(
    () => ({
      state: call.state,
      captions: call.captions,
      artifacts: call.artifacts,
      activities: call.activities,
      target,
      isActive: target !== null,
      startedAt,
      pendingSwitch,
      start,
      requestCall,
      confirmSwitch,
      cancelSwitch,
      resumable,
      resumeCall,
      dismissResume,
      end,
      toggleMute,
      inputMode,
      setInputMode,
      pttHeld,
      setPttHeld,
      enableAudio,
      getMicLevel,
      getPersonaLevel,
    }),
    [
      call.state,
      call.captions,
      call.artifacts,
      call.activities,
      target,
      startedAt,
      pendingSwitch,
      start,
      requestCall,
      confirmSwitch,
      cancelSwitch,
      resumable,
      resumeCall,
      dismissResume,
      end,
      toggleMute,
      inputMode,
      setInputMode,
      pttHeld,
      enableAudio,
      getMicLevel,
      getPersonaLevel,
    ],
  );

  return (
    <CallSessionContext.Provider value={value}>
      {children}
    </CallSessionContext.Provider>
  );
}

/**
 * Read the app-level call session. Throws outside a {@link CallSessionProvider}
 * — every authenticated surface is inside `AppShell`, so a missing provider is a
 * wiring bug, not a valid "no context" state (unlike `usePersona`, which is
 * legitimately route-optional).
 */
export function useCallSession(): CallSession {
  const ctx = useContext(CallSessionContext);
  if (ctx === null) {
    throw new Error("useCallSession must be used within a CallSessionProvider");
  }
  return ctx;
}
