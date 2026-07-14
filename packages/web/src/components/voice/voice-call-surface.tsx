"use client";

/**
 * Spec V6 B5 + C3 / Spec V7 D-V7-2 — the full call surface.
 *
 * The full-surface "call with the persona" view (D-V6-4): the Identity Orb is
 * the hero, with the persona's identity present, honest phase/failure states
 * (D-V6-5), the autoplay affordance, and mute / end controls.
 *
 * **V7 (T3): this surface BINDS the hoisted call session — it no longer owns a
 * `Room`.** It reads the live state from {@link useCallSession} instead of
 * instantiating `useVoiceCall`, so the call lives in the app-level provider and
 * survives navigation; this surface is just its expanded projection (the mini-bar
 * is the collapsed one). **R11-B7 (owner-ruled): auto-join on mount.** Arriving
 * on this route IS the intent to call, so there is no dead "Talk to {persona}"
 * start stop — the session starts on entry (fired once; never re-dialed after an
 * end). The live view's autoplay recovery (`needsAudioGesture` → "Enable audio")
 * covers the gesture the old explicit button provided, and `getUserMedia`'s own
 * permission prompt covers the mic. On end, the surface redirects straight to the
 * transcript (the chat thread) — no dead "call ended" screen either.
 * HARD GUARD: this surface holds NO `Room` and NO `<audio>` — those live in the
 * provider (and the audio sinks in `document.body`), never in this route — so the
 * route unmounting (or being hidden by a future Cache Components `<Activity>`)
 * cannot pause the call.
 */

import { ArrowLeft, Captions, Phone, PhoneOff } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";
import {
  FileRendererProvider,
  useFileRenderer,
} from "@/components/chat/file-renderer-context";
import { FileRendererPanel } from "@/components/chat/file-renderer-panel";
import { EmptyState } from "@/components/patterns/empty-state";
import { Button, buttonVariants } from "@/components/ui/button";
import { IdentityOrb } from "@/components/voice/identity-orb";
import { InputModeToggle, MicControl } from "@/components/voice/mic-control";
import { VoiceCaptions } from "@/components/voice/voice-captions";
import { VoiceTranscriptPanel } from "@/components/voice/voice-transcript-panel";
import { formatCallDuration } from "@/lib/calls";
import { personaIdentityStyle } from "@/lib/persona-identity";
import type { CallTarget } from "@/lib/voice/call-session-context";
import { useCallSession } from "@/lib/voice/call-session-context";
import { usePersonaAvatarSrc } from "@/lib/voice/use-persona-avatar-src";
import type { VoiceArtifact } from "@/lib/voice/voice-events";

export interface VoiceCallSurfaceProps {
  persona: {
    id: string;
    name: string;
    avatarUrl?: string | null;
    role?: string;
  };
  conversationId: string;
}

export function VoiceCallSurface({
  persona,
  conversationId,
}: VoiceCallSurfaceProps): React.JSX.Element | null {
  const t = useTranslations("voice");
  const router = useRouter();
  const {
    state,
    captions,
    artifacts,
    activities,
    isActive,
    startedAt,
    start,
    end,
    enableAudio,
    getMicLevel,
    getPersonaLevel,
  } = useCallSession();
  const [captionsOn, setCaptionsOn] = useState(true);

  // R9-029: the call-stage duration display — the mini-bar's own tick pattern
  // (a 1s interval while active), reusing its SAME `formatCallDuration` (m:ss).
  const [now, setNow] = useState<number | null>(null);
  useEffect(() => {
    if (!isActive) return;
    setNow(Date.now());
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [isActive]);
  const elapsed =
    startedAt !== null && now !== null
      ? formatCallDuration(Math.floor((now - startedAt) / 1000))
      : null;

  // Resolve the persona's avatar (a Spec-29 Bearer-auth workspace ref, or a
  // direct URL) to a loadable src so it can be the orb's core (D-V6-3).
  const avatarSrc = usePersonaAvatarSrc(persona.id, persona.avatarUrl);

  const target: CallTarget = {
    personaId: persona.id,
    conversationId,
    personaName: persona.name,
    personaAvatarUrl: persona.avatarUrl ?? undefined,
    personaRole: persona.role,
  };

  const backHref = `/chat/${conversationId}`;

  // R11-B7 (owner-ruled): auto-join on mount — no dead "start the call" stop.
  // Fires exactly once; skips when we mounted into a live call (just project it)
  // or a terminal phase (never re-dial an ended/dropped/errored call). start +
  // target ride refs so a fresh identity per render can't re-run the effect.
  const autoJoinedRef = useRef(false);
  const startRef = useRef(start);
  startRef.current = start;
  const targetRef = useRef(target);
  targetRef.current = target;
  useEffect(() => {
    if (autoJoinedRef.current) return;
    autoJoinedRef.current = true;
    if (isActive || state.phase !== "idle") return;
    startRef.current(targetRef.current);
  }, [isActive, state.phase]);

  // R11-B7 (owner-ruled): on end, go straight to the transcript — the chat
  // thread, where the call persists and CallRecap renders — instead of a dead
  // "call ended" screen. Covers a persona/system-initiated end; `handleEnd`
  // covers the manual End button. Guarded so it fires once per mount.
  const endRedirectedRef = useRef(false);
  useEffect(() => {
    if (state.phase === "ended" && !endRedirectedRef.current) {
      endRedirectedRef.current = true;
      router.replace(backHref);
    }
  }, [state.phase, router, backHref]);

  const header = (
    <>
      <Link
        href={backHref}
        aria-label={t("back")}
        className="v-iconbtn absolute top-4 left-4 z-10"
      >
        <ArrowLeft aria-hidden />
      </Link>
      <header className="v-voice__head">
        <div className="v-voice__title">
          {t.rich("callWith", {
            name: persona.name,
            hl: (chunks) => <span className="v-id-underline">{chunks}</span>,
          })}
        </div>
        {persona.role ? (
          <div className="v-voice__role">{persona.role}</div>
        ) : null}
      </header>
    </>
  );

  // R11-B7: no interactive "Talk to {persona}" stop — a call auto-joins on
  // entry. This frame only shows for the instant before `start()` flips the
  // phase to "connecting"; a calm placeholder, never a dead step. (A terminal
  // phase — ended/dropped/error — is handled by the redirect / EmptyState paths
  // below and never lands here as inactive.)
  if (!isActive) {
    return (
      <div className="v-voice" style={personaIdentityStyle(persona)}>
        <div
          className="v-voice__bg"
          style={{
            background:
              "radial-gradient(50% 50% at 50% 42%, oklch(0.62 0.13 var(--identity-h) / 0.14), transparent 70%)",
          }}
        />
        {header}
        <div className="v-voice__status relative z-[1]" aria-live="polite">
          {t("connecting")}
        </div>
      </div>
    );
  }

  const stateLabel =
    state.agentState === "thinking"
      ? t("thinking")
      : state.agentState === "speaking"
        ? t("speaking")
        : t("listening");

  // `ringing` (Spec 32 greet-first) is a live phase — the orb renders while the
  // persona prepares its greeting; the mic stays gated until the greeting ends.
  const live =
    state.phase === "connected" ||
    state.phase === "reconnecting" ||
    state.phase === "ringing";

  const statusLine =
    state.phase === "connecting"
      ? t("connecting")
      : state.phase === "ringing"
        ? t("ringing", { name: persona.name })
        : state.phase === "reconnecting"
          ? t("reconnecting")
          : stateLabel;

  // Terminal phases (D-V6-5) — render an honest EmptyState instead of a dead orb.
  const terminal = buildTerminal();

  // The first active capability's label drives the live "using <X>…" badge.
  const activityLabel = activities[0]?.label ?? null;

  return (
    // V10-D-6 — the SAME conversation-scoped renderer chat uses (D-28-6). A
    // produced artifact (e.g. a generated image) is auto-opened in the panel
    // while the call runs; the panel persists across artifacts and closes on
    // unmount (fresh provider per mount). Audio narration + captions stay
    // separate surfaces — the panel only ever shows the file.
    <FileRendererProvider>
      <ArtifactAutoOpener artifacts={artifacts} />
      <FileRendererPanel personaId={persona.id} />
      {/* R9-029 — desktop (the SAME md boundary the chat right panel / R9-026's
          height fix use): the call stage stays exactly as it was (mobile is
          byte-identical — the aside is `hidden` below md, so it never joins the
          flex layout there); a full-height transcript panel sits to its right. */}
      <div className="flex h-full min-h-0 flex-col md:flex-row">
        <div
          className="v-voice min-h-0 flex-1"
          style={personaIdentityStyle(persona)}
        >
          {/* Identity-tinted backdrop — a soft radial wash in the persona's hue. */}
          <div
            className="v-voice__bg"
            style={{
              background:
                "radial-gradient(50% 50% at 50% 42%, oklch(0.62 0.13 var(--identity-h) / 0.14), transparent 70%)",
            }}
          />
          {header}

          {terminal ? (
            <EmptyState
              className="relative z-[1] w-full max-w-md"
              icon={<Phone className="size-6" aria-hidden />}
              title={terminal.title}
              description={terminal.body}
              action={terminal.action}
            />
          ) : (
            <>
              <div className="v-orb-wrap">
                <IdentityOrb
                  persona={{ id: persona.id, name: persona.name }}
                  agentState={state.agentState}
                  bargeInSignal={state.bargeInSignal}
                  getMicLevel={getMicLevel}
                  getPersonaLevel={getPersonaLevel}
                  avatarUrl={avatarSrc}
                  label={stateLabel}
                />
              </div>

              <div className="v-voice__status" aria-live="polite">
                {statusLine}
              </div>

              {/* R9-029 — the call-stage duration (kept alongside the state
                chips/controls per the mockup); mirrors the mini-bar's own
                elapsed-timer display, hidden until the first tick lands. */}
              {elapsed ? (
                <div
                  className="relative z-[1] font-mono text-muted-foreground type-caption normal-case tracking-normal"
                  aria-live="off"
                  data-slot="voice-call-duration"
                >
                  {elapsed}
                </div>
              ) : null}

              {/* V10-D-6 — the live "using <X>…" capability badge; hidden when no
              capability is in flight. Distinct from the status line (which is
              the conversational phase) and the captions (which are speech). */}
              {activityLabel ? (
                <div
                  className="relative z-[1] flex items-center gap-2 font-mono text-muted-foreground type-caption normal-case tracking-normal"
                  aria-live="polite"
                >
                  <span className="v-id-dot" />
                  {t("using", { label: activityLabel })}
                </div>
              ) : null}

              {/* R9-029: the inline caption is the MOBILE transcript surface.
                  At md+ the full transcript right panel takes over (same
                  breakpoint the aside below appears at), so hide this one
                  there — otherwise both render on desktop (the duplicate). */}
              {captionsOn ? (
                <div className="v-voice__caption md:hidden">
                  <VoiceCaptions
                    captions={captions}
                    personaName={persona.name}
                  />
                </div>
              ) : null}

              {state.needsAudioGesture ? (
                <Button
                  variant="secondary"
                  className="relative z-[1]"
                  onClick={() => void enableAudio()}
                >
                  {t("enableAudio")}
                </Button>
              ) : null}

              {live ? (
                <div className="v-voice__controls">
                  {/* D-V7-6: mute toggle, or a hold-to-talk button in push-to-talk. */}
                  <MicControl className="v-voice-ctl" />
                  <button
                    type="button"
                    className="v-voice-ctl v-voice-ctl--end"
                    onClick={() => void handleEnd()}
                    aria-label={t("end")}
                    title={t("end")}
                  >
                    <PhoneOff aria-hidden />
                  </button>
                  <button
                    type="button"
                    className="v-voice-ctl"
                    onClick={() => setCaptionsOn((c) => !c)}
                    aria-label={t("captionsLabel")}
                    title={t("captionsLabel")}
                    aria-pressed={captionsOn}
                  >
                    <Captions aria-hidden />
                  </button>
                  {/* D-V7-6: switch always-listening ↔ push-to-talk (persisted). */}
                  <InputModeToggle className="v-voice-ctl" />
                </div>
              ) : null}

              {/* The shared-memory note — voice + text are one thread (D-V6-4). */}
              <div className="relative z-[1] flex items-center gap-2 font-mono text-muted-foreground type-caption normal-case tracking-normal">
                <span className="v-id-dot" />
                {t("memoryNote")}
              </div>
            </>
          )}
        </div>
        {/* R9-029: desktop-only transcript right panel — hidden (never joins
            the flex layout) below md, so mobile keeps VoiceCaptions inline
            above, byte-identical to before this redesign. */}
        {!terminal && captionsOn ? (
          <aside
            className="hidden min-h-0 shrink-0 border-border border-l md:flex md:w-80 lg:w-96"
            aria-label={t("transcript")}
          >
            <VoiceTranscriptPanel
              captions={captions}
              persona={{
                id: persona.id,
                name: persona.name,
                avatar_url: avatarSrc,
              }}
            />
          </aside>
        ) : null}
      </div>
    </FileRendererProvider>
  );

  /** End the call and leave the call screen (voice + text are one thread). */
  async function handleEnd(): Promise<void> {
    await end();
    router.replace(backHref);
  }

  /** Resolve the terminal-phase copy + recovery action, or null if live. */
  function buildTerminal(): {
    title: string;
    body: string;
    action: React.ReactNode;
  } | null {
    if (state.phase === "error" && state.error) {
      const kind = state.error.kind;
      let action: React.ReactNode = null;
      if (kind === "unauthorized") {
        action = (
          <Link
            href="/sign-in"
            className={buttonVariants({ variant: "default", size: "lg" })}
          >
            {t("signIn")}
          </Link>
        );
      } else if (kind !== "not_found" && kind !== "credits_exhausted") {
        // mic_* / service_unavailable / unknown — retry is meaningful.
        action = (
          <Button size="lg" onClick={() => start(target)}>
            {t("retry")}
          </Button>
        );
      }
      return {
        title: t(`fail.${kind}.title`),
        body: t(`fail.${kind}.body`),
        action,
      };
    }
    if (state.phase === "dropped") {
      return {
        title: t("dropped"),
        body: t("droppedBody"),
        action: (
          <Button size="lg" onClick={() => start(target)}>
            {t("retry")}
          </Button>
        ),
      };
    }
    return null;
  }
}

/**
 * Auto-opens the LATEST produced artifact in the file-renderer panel (V10-D-6).
 * Keyed on the newest artifact's `workspacePath`, so it fires once per new
 * artifact: it opens the first when it arrives and follows subsequent ones,
 * without re-stealing focus on unrelated re-renders. Renders nothing — it only
 * drives the shared {@link useFileRenderer} state. Mounted INSIDE the provider.
 */
function ArtifactAutoOpener({
  artifacts,
}: {
  artifacts: VoiceArtifact[];
}): null {
  const { open } = useFileRenderer();
  // `mergeArtifacts` returns the same array (and so the same newest element)
  // reference on a no-op, so `latest`'s identity is stable across unrelated
  // re-renders — the effect re-fires only when a genuinely new artifact arrives.
  const latest = artifacts.at(-1) ?? null;

  useEffect(() => {
    if (latest === null) return;
    open({
      workspacePath: latest.workspacePath,
      mediaType: latest.mimeType,
      name: latest.workspacePath.split("/").pop() ?? latest.workspacePath,
    });
  }, [latest, open]);

  return null;
}
