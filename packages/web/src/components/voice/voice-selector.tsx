"use client";

/**
 * Spec V6 C2 — voice-selection-with-preview (D-V6 criterion 6).
 *
 * The V6 contribution to authoring/management (F5 / Spec 10 own those screens;
 * this is the F2-composed component they consume). Lists the provider voice
 * catalogue with hear-before-choosing previews; choosing sets the persona's
 * `VoiceSpec` (`{provider, voice_id}`). Self-contained: it fetches `/v1/voices`
 * with the user's Clerk token internally, so the host form just renders it with
 * `value` + `onChange`. Degrades honestly (loading / unavailable / error) so an
 * unconfigured TTS deployment shows a calm message, never a broken control.
 */

import { Pause, Play } from "lucide-react";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useRef, useState } from "react";
import { useAuth } from "@/auth";
import { CAPABILITY_SCROLL_LIST_CLASS } from "@/components/personas/capability-list";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import {
  fetchVoices,
  type VoiceSummary,
  voiceDisplayName,
} from "@/lib/voice/voices";

const TEMPLATE = process.env.NEXT_PUBLIC_CLERK_JWT_TEMPLATE;

/** The `{provider, voice_id}` the persona's `identity.voice` stores. */
export interface VoiceValue {
  provider: string;
  voice_id: string;
}

export interface VoiceSelectorProps {
  /** The currently-selected voice id (from `identity.voice.voice_id`), if any. */
  value?: string | null;
  /** Set the persona's voice (or clear it to use the global default). */
  onChange: (voice: VoiceValue | null) => void;
  /**
   * Spec 32 — the persona's declared `language_default`. Only voices that speak
   * it are listed (so a voice that can't speak the persona's language can't be
   * picked). The list re-fetches when this changes (modular).
   */
  language?: string | null;
}

type LoadState =
  | { status: "loading" }
  | { status: "error" }
  | { status: "ready"; provider: string | null; voices: VoiceSummary[] };

export function VoiceSelector({
  value,
  onChange,
  language,
}: VoiceSelectorProps): React.JSX.Element {
  const t = useTranslations("voice");
  const { getToken } = useAuth();
  const [load, setLoad] = useState<LoadState>({ status: "loading" });
  const [playing, setPlaying] = useState<string | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);

  // Keep the latest `getToken` in a ref so the fetch effect does NOT depend on
  // its identity. Clerk memoises `getToken` in production, but a host (or a
  // test mock) that returns a fresh function each render would otherwise make
  // `[getToken, ...]` change every render → effect re-runs → setLoad →
  // re-render → infinite loop (heap exhaustion). The voices only need to
  // re-fetch when the persona's `language` changes, never on token churn.
  const getTokenRef = useRef(getToken);
  getTokenRef.current = getToken;

  useEffect(() => {
    const controller = new AbortController();
    let cancelled = false;
    setLoad({ status: "loading" });
    (async () => {
      try {
        const list = await fetchVoices({
          getToken: () =>
            getTokenRef.current(TEMPLATE ? { template: TEMPLATE } : undefined),
          signal: controller.signal,
          language,
        });
        if (!cancelled) setLoad({ status: "ready", ...list });
      } catch {
        if (!cancelled) setLoad({ status: "error" });
      }
    })();
    return () => {
      cancelled = true;
      controller.abort();
    };
    // Re-fetch when the persona's language changes (modular filter, Spec 32).
  }, [language]);

  // Stop any preview on unmount.
  useEffect(() => {
    return () => {
      audioRef.current?.pause();
      audioRef.current = null;
    };
  }, []);

  const togglePreview = useCallback(
    (voiceId: string, previewUrl: string) => {
      if (playing === voiceId) {
        audioRef.current?.pause();
        setPlaying(null);
        return;
      }
      audioRef.current?.pause();
      const audio = new Audio(previewUrl);
      audioRef.current = audio;
      audio.onended = () => setPlaying((p) => (p === voiceId ? null : p));
      void audio.play().catch(() => setPlaying(null));
      setPlaying(voiceId);
    },
    [playing],
  );

  if (load.status === "loading") {
    return (
      <p className="text-sm text-muted-foreground">{t("voicesLoading")}</p>
    );
  }
  if (load.status === "error") {
    return <p className="text-sm text-muted-foreground">{t("voicesError")}</p>;
  }
  if (load.provider === null || load.voices.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">{t("voicesUnavailable")}</p>
    );
  }

  const provider = load.provider;

  return (
    <ul className={cn("flex flex-col gap-1.5", CAPABILITY_SCROLL_LIST_CLASS)}>
      {/* Default option — clears the persona's voice (global default). */}
      <li>
        <VoiceRow
          selected={!value}
          label={t("voiceDefault")}
          onSelect={() => onChange(null)}
          selectedLabel={t("voiceSelected")}
        />
      </li>
      {load.voices.map((voice) => (
        <li key={voice.voice_id}>
          <VoiceRow
            selected={value === voice.voice_id}
            label={voiceDisplayName(voice)}
            description={voice.description ?? undefined}
            gender={voice.gender}
            language={voice.language}
            onSelect={() => onChange({ provider, voice_id: voice.voice_id })}
            selectedLabel={t("voiceSelected")}
            preview={
              voice.preview_url
                ? {
                    playing: playing === voice.voice_id,
                    onToggle: () =>
                      togglePreview(
                        voice.voice_id,
                        voice.preview_url as string,
                      ),
                    playLabel: t("voicePreview"),
                    stopLabel: t("voiceStop"),
                  }
                : undefined
            }
          />
        </li>
      ))}
    </ul>
  );
}

interface VoiceRowProps {
  selected: boolean;
  label: string;
  description?: string;
  /** Provider gender tag (e.g. `feminine`) — shown as a chip when meaningful. */
  gender?: string;
  /** Provider language tag (e.g. `ar`) — shown as a chip when present. */
  language?: string | null;
  selectedLabel: string;
  onSelect: () => void;
  preview?: {
    playing: boolean;
    onToggle: () => void;
    playLabel: string;
    stopLabel: string;
  };
}

function VoiceRow({
  selected,
  label,
  description,
  gender,
  language,
  selectedLabel,
  onSelect,
  preview,
}: VoiceRowProps): React.JSX.Element {
  const showGender = gender && gender !== "unspecified";
  return (
    <div
      className={cn(
        "flex items-center gap-2 rounded-lg border px-3 py-2",
        selected ? "border-primary bg-primary/5" : "border-border",
      )}
    >
      <button
        type="button"
        onClick={onSelect}
        aria-pressed={selected}
        className="flex-1 text-left"
      >
        <span className="text-sm font-medium">{label}</span>
        {showGender || language ? (
          <span className="mt-0.5 flex flex-wrap items-center gap-1">
            {showGender ? <VoiceTag>{gender}</VoiceTag> : null}
            {language ? <VoiceTag>{language}</VoiceTag> : null}
          </span>
        ) : null}
        {description ? (
          <span className="block text-xs text-muted-foreground">
            {description}
          </span>
        ) : null}
      </button>
      {selected ? (
        <span className="text-xs text-muted-foreground">{selectedLabel}</span>
      ) : null}
      {preview ? (
        <Button
          type="button"
          variant="secondary"
          size="icon-sm"
          aria-label={preview.playing ? preview.stopLabel : preview.playLabel}
          onClick={preview.onToggle}
        >
          {preview.playing ? <Pause /> : <Play />}
        </Button>
      ) : null}
    </div>
  );
}

/** A small muted chip for a voice's gender / language tag. */
function VoiceTag({ children }: { children: string }): React.JSX.Element {
  return (
    <span className="type-caption rounded bg-muted px-1.5 py-px font-medium text-muted-foreground">
      {children}
    </span>
  );
}
