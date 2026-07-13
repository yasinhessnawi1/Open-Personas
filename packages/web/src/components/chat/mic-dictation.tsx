"use client";

/**
 * R9-025a — ONE reusable mic-dictation component: record → transcribe →
 * insert the editable transcript at the caret. Mounted in (a) the chat
 * composer and (b) the persona-authoring wizard's description field — both
 * hosts own a plain `[value, setValue]` string pair, so this component takes
 * `value`/`onChange`/`textareaRef` directly and does the caret-aware
 * insertion itself (no per-host duplication).
 *
 * State machine: idle → recording (MediaRecorder) → transcribing (POST
 * /v1/stt via the api proxy) → idle (transcript inserted). `permissionDenied`
 * is a distinct, VISIBLE error state (user-actionable — grant mic access in
 * browser settings); `unavailable` (no MediaRecorder support, or the proxy's
 * 503 `voice_unavailable`) HIDES the affordance entirely — the R9-025
 * RESCOPE's fail-soft contract ("mic hidden when feature-absent").
 *
 * One-shot v1 (record-stop → text; full-clip audio) per the RESCOPE —
 * streaming dictation is a flagged follow-up.
 */

import { Loader2, Mic, Square } from "lucide-react";
import { useTranslations } from "next-intl";
import {
  type RefObject,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { useAuth } from "@/auth";
import { buttonVariants } from "@/components/ui/button";
import { ApiError } from "@/lib/api/client";
import { cn } from "@/lib/utils";
import { transcribeAudio } from "@/lib/voice/stt";

const TEMPLATE = process.env.NEXT_PUBLIC_CLERK_JWT_TEMPLATE;

/** Provider-supported containers, most-preferred first (Safari lacks webm). */
const PREFERRED_MIME_TYPES = [
  "audio/webm;codecs=opus",
  "audio/webm",
  "audio/mp4",
  "audio/ogg",
];

function pickSupportedMimeType(): string | undefined {
  if (
    typeof MediaRecorder === "undefined" ||
    typeof MediaRecorder.isTypeSupported !== "function"
  ) {
    return undefined;
  }
  for (const type of PREFERRED_MIME_TYPES) {
    if (MediaRecorder.isTypeSupported(type)) return type;
  }
  return undefined;
}

/** Insert `insert` at the textarea's current caret (or append when unknown). */
function insertAtCaret(
  value: string,
  insert: string,
  textarea: HTMLTextAreaElement | null,
): { next: string; caret: number } {
  // Only trust `selectionStart`/`selectionEnd` while the textarea actually
  // HAS focus — an unfocused textarea's selection defaults to 0/0 (start of
  // text), which would prepend the dictated transcript before existing
  // content on the (common) "click the mic without first clicking into the
  // field" path. Append at the end in that case — the safe, expected default.
  const focused = !!textarea && document.activeElement === textarea;
  const start = focused
    ? (textarea?.selectionStart ?? value.length)
    : value.length;
  const end = focused ? (textarea?.selectionEnd ?? value.length) : value.length;
  const before = value.slice(0, start);
  const after = value.slice(end);
  const needsSpaceBefore = before.length > 0 && !/\s$/.test(before);
  const insertText = (needsSpaceBefore ? " " : "") + insert;
  return {
    next: before + insertText + after,
    caret: (before + insertText).length,
  };
}

type DictationState =
  | "idle"
  | "recording"
  | "transcribing"
  | "permissionDenied"
  | "unavailable";

export interface MicDictationProps {
  /** The host's current text value (composer input / description field). */
  value: string;
  /** The host's setter — receives the value WITH the transcript inserted. */
  onChange: (next: string) => void;
  /** The host's textarea ref — read for caret position, refocused after insert. */
  textareaRef: RefObject<HTMLTextAreaElement | null>;
  disabled?: boolean;
  className?: string;
  /**
   * Optional context language hint (R9-025 reopen — context-pinned
   * dictation): the chat composer passes the conversation persona's
   * declared language; the persona-authoring mic passes the active UI
   * locale. Threaded straight through to {@link transcribeAudio} — see its
   * own doc for the shape/omission contract. Omitted → auto-detect.
   */
  language?: string;
}

export function MicDictation({
  value,
  onChange,
  textareaRef,
  disabled = false,
  className,
  language,
}: MicDictationProps) {
  const t = useTranslations("mic");
  const { getToken } = useAuth();
  const [state, setState] = useState<DictationState>("idle");
  const recorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const streamRef = useRef<MediaStream | null>(null);
  const pendingCaretRef = useRef<number | null>(null);
  // Mirrors value/onChange in refs so the MediaRecorder's onstop callback
  // (registered once per recording, closing over whatever `value` was at
  // record-START) always inserts against the LATEST text, not a stale one.
  const valueRef = useRef(value);
  valueRef.current = value;
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;

  // Restore focus + caret position after an inserted transcript re-renders
  // the host's textarea (setState is async, so this runs post-render).
  // `value` is the deliberate re-run TRIGGER (the host re-rendering with the
  // inserted transcript) even though the body reads the refs, not `value`.
  // biome-ignore lint/correctness/useExhaustiveDependencies: see comment above — removing `value` would stop this firing on insert.
  useEffect(() => {
    if (pendingCaretRef.current !== null && textareaRef.current) {
      const pos = pendingCaretRef.current;
      textareaRef.current.focus();
      textareaRef.current.setSelectionRange(pos, pos);
      pendingCaretRef.current = null;
    }
  }, [value, textareaRef]);

  const stopStream = useCallback(() => {
    for (const track of streamRef.current?.getTracks() ?? []) track.stop();
    streamRef.current = null;
  }, []);

  // Stop any live mic stream on unmount (navigate away mid-recording).
  useEffect(() => stopStream, [stopStream]);

  const insertTranscript = useCallback(
    (transcript: string) => {
      const trimmed = transcript.trim();
      if (!trimmed) return;
      const { next, caret } = insertAtCaret(
        valueRef.current,
        trimmed,
        textareaRef.current,
      );
      pendingCaretRef.current = caret;
      onChangeRef.current(next);
    },
    [textareaRef],
  );

  const transcribe = useCallback(async () => {
    setState("transcribing");
    stopStream();
    const mimeType = recorderRef.current?.mimeType || "audio/webm";
    const blob = new Blob(chunksRef.current, { type: mimeType });
    chunksRef.current = [];
    recorderRef.current = null;
    try {
      const transcript = await transcribeAudio(blob, {
        getToken: () => getToken(TEMPLATE ? { template: TEMPLATE } : undefined),
        language,
      });
      insertTranscript(transcript);
      setState("idle");
    } catch (e) {
      if (e instanceof ApiError && e.status === 503) {
        setState("unavailable");
        return;
      }
      setState("idle");
    }
  }, [stopStream, insertTranscript, getToken, language]);

  const startRecording = useCallback(async () => {
    if (
      typeof navigator === "undefined" ||
      !navigator.mediaDevices?.getUserMedia ||
      typeof MediaRecorder === "undefined"
    ) {
      setState("unavailable");
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      streamRef.current = stream;
      chunksRef.current = [];
      const mimeType = pickSupportedMimeType();
      const recorder = new MediaRecorder(
        stream,
        mimeType ? { mimeType } : undefined,
      );
      recorderRef.current = recorder;
      recorder.ondataavailable = (e) => {
        if (e.data.size > 0) chunksRef.current.push(e.data);
      };
      recorder.onstop = () => void transcribe();
      recorder.start();
      setState("recording");
    } catch {
      // NotAllowedError (denied) / NotFoundError (no mic) / any other
      // getUserMedia rejection — a visible, user-actionable state, distinct
      // from "not configured" (which hides the button entirely).
      stopStream();
      setState("permissionDenied");
    }
  }, [transcribe, stopStream]);

  const stopRecording = useCallback(() => {
    recorderRef.current?.stop();
  }, []);

  const onToggle = useCallback(() => {
    if (state === "recording") {
      stopRecording();
      return;
    }
    if (state === "idle" || state === "permissionDenied") {
      void startRecording();
    }
  }, [state, startRecording, stopRecording]);

  // R9-025 RESCOPE fail-soft: the deployment has no voice service (or this
  // browser can't record at all) — hide the affordance, never a dead/broken
  // button.
  if (state === "unavailable") return null;

  const label =
    state === "recording"
      ? t("stop")
      : state === "transcribing"
        ? t("transcribing")
        : state === "permissionDenied"
          ? t("permissionDenied")
          : t("start");

  return (
    <button
      type="button"
      onClick={onToggle}
      disabled={disabled || state === "transcribing"}
      aria-label={label}
      title={label}
      aria-pressed={state === "recording"}
      data-slot="mic-dictation"
      data-state={state}
      className={cn(
        buttonVariants({ variant: "ghost", size: "icon-sm" }),
        state === "recording" && "text-destructive",
        className,
      )}
    >
      {state === "transcribing" ? (
        <Loader2 className="size-4 animate-spin" aria-hidden="true" />
      ) : state === "recording" ? (
        <Square className="size-4 animate-pulse" aria-hidden="true" />
      ) : (
        <Mic className="size-4" aria-hidden="true" />
      )}
    </button>
  );
}
