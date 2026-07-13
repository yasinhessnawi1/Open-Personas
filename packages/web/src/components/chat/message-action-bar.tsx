"use client";

/**
 * R9-025a — the hover/focus message action bar: copy (both roles), retry
 * (persona messages — R9-025 leg C: a REAL server-side regenerate, see
 * use-chat.ts's `regenerate`), read aloud (persona messages — plays the
 * persona's REAL voice via the api TTS proxy). R9-025 leg C also adds edit
 * (user messages — opens the inline edit form owned by message-element.tsx's
 * `UserMessage`). Visible on hover/focus of the message row (the parent
 * applies `group`); buttons stay in the tab order regardless (opacity-only
 * hide, not `display:none`) so keyboard users can reach them.
 */

import {
  Check,
  ChevronDown,
  Copy,
  FileOutput,
  Loader2,
  Pencil,
  RotateCcw,
  Square,
  Volume2,
} from "lucide-react";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useRef, useState } from "react";
import { useAuth } from "@/auth";
import { buttonVariants } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { ApiError } from "@/lib/api/client";
import type { TurnIntoFileFormat } from "@/lib/turn-into-file";
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
  /** Persona role only. Omit to hide the retry button (e.g. no seam wired, or not the tail message). */
  onRetry?: () => void;
  retryDisabled?: boolean;
  /**
   * R9-025 leg C — user role only. Omit to hide the edit button entirely
   * (e.g. no seam wired, or — per the v1 conversation-TAIL-only scope — this
   * isn't the last user message). Opens the inline edit form; no arguments
   * (the caller, `UserMessage`, owns the draft-text state).
   */
  onEdit?: () => void;
  /** Mirrors `retryDisabled` — disabled while ANY turn is active. */
  editDisabled?: boolean;
  /** Persona role only — required for read-aloud (resolves the persona's voice). */
  personaId?: string;
  /**
   * R9-025b — persona role only. Omit to hide "Turn into file" entirely (e.g.
   * no seam wired). Fires immediately with the given format — ``"auto"`` on a
   * plain click of the main button (the calm default), or an explicit
   * pdf/md/xlsx/csv pick from the disclosure menu.
   */
  onTurnIntoFile?: (format: TurnIntoFileFormat) => void;
  /** Mirrors ``retryDisabled`` — disabled while ANY turn is active (2a's `streaming` guard). */
  turnIntoFileDisabled?: boolean;
  className?: string;
}

export function MessageActionBar({
  messageRole,
  content,
  onRetry,
  retryDisabled,
  onEdit,
  editDisabled,
  personaId,
  onTurnIntoFile,
  turnIntoFileDisabled,
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
      {messageRole === "user" && onEdit ? (
        <EditButton onEdit={onEdit} disabled={!!editDisabled} />
      ) : null}
      {messageRole === "persona" && onRetry ? (
        <RetryButton onRetry={onRetry} disabled={!!retryDisabled} />
      ) : null}
      {messageRole === "persona" && personaId ? (
        <ReadAloudButton personaId={personaId} text={content} />
      ) : null}
      {messageRole === "persona" && onTurnIntoFile ? (
        <TurnIntoFileControl
          onTurnIntoFile={onTurnIntoFile}
          disabled={!!turnIntoFileDisabled}
        />
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

function EditButton({
  onEdit,
  disabled,
}: {
  onEdit: () => void;
  disabled: boolean;
}) {
  const t = useTranslations("chat");
  const label = t("actions.edit");
  return (
    <button
      type="button"
      onClick={onEdit}
      disabled={disabled}
      aria-label={label}
      title={label}
      data-slot="message-action-edit"
      className={buttonVariants({ variant: "ghost", size: "icon-sm" })}
    >
      <Pencil className="size-4" aria-hidden="true" />
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

/**
 * R9-025b — "Turn into file": a split control. The main button fires
 * IMMEDIATELY with the calm default (``format="auto"``) — no menu, no
 * confirmation, one click. A small chevron disclosure opens a menu offering
 * the explicit pdf/md/xlsx/csv picks for when the caller wants a specific
 * format. Both paths call the SAME ``onTurnIntoFile`` callback — the parent
 * (chat-window) owns the actual fetch + the optimistic toast.
 */
function TurnIntoFileControl({
  onTurnIntoFile,
  disabled,
}: {
  onTurnIntoFile: (format: TurnIntoFileFormat) => void;
  disabled: boolean;
}) {
  const t = useTranslations("chat");
  const mainLabel = t("actions.turnIntoFile.label");
  const menuLabel = t("actions.turnIntoFile.formatMenu");
  return (
    <div
      className="flex items-center"
      data-slot="message-action-turn-into-file"
    >
      <button
        type="button"
        onClick={() => onTurnIntoFile("auto")}
        disabled={disabled}
        aria-label={mainLabel}
        title={mainLabel}
        data-slot="message-action-turn-into-file-auto"
        className={buttonVariants({ variant: "ghost", size: "icon-sm" })}
      >
        <FileOutput className="size-4" aria-hidden="true" />
      </button>
      <DropdownMenu>
        <DropdownMenuTrigger
          disabled={disabled}
          aria-label={menuLabel}
          title={menuLabel}
          data-slot="message-action-turn-into-file-menu-trigger"
          className={cn(
            buttonVariants({ variant: "ghost", size: "icon-sm" }),
            "w-4",
          )}
        >
          <ChevronDown className="size-3" aria-hidden="true" />
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuItem onClick={() => onTurnIntoFile("pdf")}>
            {t("actions.turnIntoFile.format.pdf")}
          </DropdownMenuItem>
          <DropdownMenuItem onClick={() => onTurnIntoFile("md")}>
            {t("actions.turnIntoFile.format.md")}
          </DropdownMenuItem>
          <DropdownMenuItem onClick={() => onTurnIntoFile("xlsx")}>
            {t("actions.turnIntoFile.format.xlsx")}
          </DropdownMenuItem>
          <DropdownMenuItem onClick={() => onTurnIntoFile("csv")}>
            {t("actions.turnIntoFile.format.csv")}
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}
