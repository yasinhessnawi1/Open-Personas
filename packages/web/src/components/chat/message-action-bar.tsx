"use client";

/**
 * R9-025a — the hover/focus message action bar: copy (both roles), retry
 * (persona messages — client-side re-send of the preceding user message as
 * a NEW turn; see message-element.tsx's retry-seam decision note), read
 * aloud (persona messages — plays the persona's REAL voice via the api TTS
 * proxy). Visible on hover/focus of the message row (the parent applies
 * `group`); buttons stay in the tab order regardless (opacity-only hide, not
 * `display:none`) so keyboard users can reach them.
 */

import { Check, Copy, Loader2, RotateCcw, Square, Volume2 } from "lucide-react";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useRef, useState } from "react";
import { useAuth } from "@/auth";
import { buttonVariants } from "@/components/ui/button";
import { ApiError } from "@/lib/api/client";
import { cn } from "@/lib/utils";
import { fetchPersonaTts } from "@/lib/voice/tts";

const TEMPLATE = process.env.NEXT_PUBLIC_CLERK_JWT_TEMPLATE;

export interface MessageActionBarProps {
  // NOTE: named `messageRole` (not `role`) so it can never be mistaken for —
  // or trip a lint rule meant for — the DOM/ARIA `role` attribute; this is a
  // plain data prop on a React component, not an accessibility attribute.
  messageRole: "user" | "persona";
  /** The message's raw text (markdown source) — copy target + read-aloud input. */
  content: string;
  /** Persona role only. Omit to hide the retry button (e.g. no seam wired). */
  onRetry?: () => void;
  retryDisabled?: boolean;
  /** Persona role only — required for read-aloud (resolves the persona's voice). */
  personaId?: string;
  className?: string;
}

export function MessageActionBar({
  messageRole,
  content,
  onRetry,
  retryDisabled,
  personaId,
  className,
}: MessageActionBarProps) {
  return (
    <div
      className={cn(
        "flex items-center gap-0.5 opacity-0 transition-opacity duration-[var(--motion-duration-fast)] focus-within:opacity-100 group-hover:opacity-100 group-focus-within:opacity-100",
        className,
      )}
      data-slot="message-action-bar"
    >
      <CopyMessageButton content={content} />
      {messageRole === "persona" && onRetry ? (
        <RetryButton onRetry={onRetry} disabled={!!retryDisabled} />
      ) : null}
      {messageRole === "persona" && personaId ? (
        <ReadAloudButton personaId={personaId} text={content} />
      ) : null}
    </div>
  );
}

function CopyMessageButton({ content }: { content: string }) {
  const t = useTranslations("chat");
  const [copied, setCopied] = useState(false);
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(
    () => () => {
      if (timeoutRef.current) clearTimeout(timeoutRef.current);
    },
    [],
  );

  const onCopy = useCallback(() => {
    if (!content) return;
    void navigator.clipboard.writeText(content).then(() => {
      setCopied(true);
      timeoutRef.current = setTimeout(() => setCopied(false), 1500);
    });
  }, [content]);

  const label = copied ? t("actions.copied") : t("actions.copy");
  return (
    <button
      type="button"
      onClick={onCopy}
      aria-label={label}
      title={label}
      data-slot="message-action-copy"
      className={buttonVariants({ variant: "ghost", size: "icon-sm" })}
    >
      {copied ? (
        <Check className="size-4" aria-hidden="true" />
      ) : (
        <Copy className="size-4" aria-hidden="true" />
      )}
    </button>
  );
}

function RetryButton({
  onRetry,
  disabled,
}: {
  onRetry: () => void;
  disabled: boolean;
}) {
  const t = useTranslations("chat");
  const label = t("actions.retry");
  return (
    <button
      type="button"
      onClick={onRetry}
      disabled={disabled}
      aria-label={label}
      title={label}
      data-slot="message-action-retry"
      className={buttonVariants({ variant: "ghost", size: "icon-sm" })}
    >
      <RotateCcw className="size-4" aria-hidden="true" />
    </button>
  );
}

type ReadAloudState = "idle" | "loading" | "playing" | "unavailable";

function ReadAloudButton({
  personaId,
  text,
}: {
  personaId: string;
  text: string;
}) {
  const t = useTranslations("chat");
  const { getToken } = useAuth();
  const [state, setState] = useState<ReadAloudState>("idle");
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const urlRef = useRef<string | null>(null);

  const releaseUrl = useCallback(() => {
    if (urlRef.current) {
      URL.revokeObjectURL(urlRef.current);
      urlRef.current = null;
    }
  }, []);

  useEffect(
    () => () => {
      audioRef.current?.pause();
      releaseUrl();
    },
    [releaseUrl],
  );

  const stop = useCallback(() => {
    audioRef.current?.pause();
    if (audioRef.current) audioRef.current.currentTime = 0;
    setState("idle");
  }, []);

  const play = useCallback(async () => {
    if (state === "playing") {
      stop();
      return;
    }
    if (!text.trim()) return;
    setState("loading");
    try {
      const blob = await fetchPersonaTts(personaId, text, {
        getToken: () => getToken(TEMPLATE ? { template: TEMPLATE } : undefined),
      });
      const url = URL.createObjectURL(blob);
      releaseUrl();
      urlRef.current = url;
      const audio = audioRef.current ?? new Audio();
      audioRef.current = audio;
      audio.src = url;
      audio.onended = () => setState("idle");
      await audio.play();
      setState("playing");
    } catch (e) {
      if (e instanceof ApiError && e.status === 503) {
        setState("unavailable");
        return;
      }
      setState("idle");
    }
  }, [state, text, personaId, getToken, stop, releaseUrl]);

  // R9-025 RESCOPE fail-soft: no voice service configured — hide, never a
  // dead/broken button.
  if (state === "unavailable") return null;

  const label =
    state === "playing"
      ? t("actions.stopReading")
      : state === "loading"
        ? t("actions.loadingAudio")
        : t("actions.readAloud");

  return (
    <button
      type="button"
      onClick={() => void play()}
      disabled={state === "loading"}
      aria-label={label}
      title={label}
      data-slot="message-action-read-aloud"
      className={buttonVariants({ variant: "ghost", size: "icon-sm" })}
    >
      {state === "loading" ? (
        <Loader2 className="size-4 animate-spin" aria-hidden="true" />
      ) : state === "playing" ? (
        <Square className="size-4" aria-hidden="true" />
      ) : (
        <Volume2 className="size-4" aria-hidden="true" />
      )}
    </button>
  );
}
