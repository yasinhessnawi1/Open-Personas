"use client";

/**
 * Issue #16 - the attach control the task and routine hand-off dialogs share.
 *
 * Chat has let you drop a file on a message for a long time; handing a persona a task had
 * no such door, so people pasted file contents into the goal. This is the same door: the
 * same `POST /v1/personas/:id/uploads` endpoint, the same `uploads/<hash>.<ext>` workspace
 * ref, and the ref travels on the task contract so the persona actually opens the file
 * when the work runs. For a routine, every occurrence reads the same files.
 *
 * The hook owns the upload state so both dialogs behave identically: a chip appears while
 * the file is going up, turns into a real attachment when it lands, and says so plainly
 * when it does not. Files picked before a persona is chosen wait rather than fail, because
 * the upload is persona-scoped and the dialogs let you type the goal first.
 */

import { Loader2, Paperclip, X } from "lucide-react";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useId, useRef, useState } from "react";
import { useAuth } from "@/auth";
import { buttonVariants } from "@/components/ui/button";
import { ApiError, type TokenGetter } from "@/lib/api/client";
import { DOCUMENT_EXTENSIONS, IMAGE_MEDIA_TYPES } from "@/lib/api/limits";
import { type TaskAttachmentRef, uploadTaskAttachment } from "@/lib/upload";
import { cn } from "@/lib/utils";

const TEMPLATE = process.env.NEXT_PUBLIC_CLERK_JWT_TEMPLATE;

/** Mirrors `StartRunRequest.attachments` / `ScheduleCreateRequest.attachments` max_length. */
export const MAX_HAND_OFF_ATTACHMENTS = 10;

/** One row in the chip list: pending upload, landed file, or a failure with its reason. */
export interface HandOffAttachment {
  id: string;
  filename: string;
  state: "uploading" | "ready" | "error";
  /** Present once the upload landed - this is what the create payload carries. */
  ref?: TaskAttachmentRef;
  /** Present on failure, as the detail the chip's tooltip shows. */
  detail?: string;
}

export interface HandOffAttachmentsState {
  attachments: HandOffAttachment[];
  /** The refs to send with the create call (only the ones that actually landed). */
  refs: TaskAttachmentRef[];
  /** True while any file is still going up: the dialogs hold the submit until it settles. */
  busy: boolean;
  /** How many more files the API will take. The button stops offering at zero. */
  remaining: number;
  attach: (file: File) => void;
  remove: (id: string) => void;
}

/**
 * Own the attachment list for one hand-off dialog.
 *
 * @param personaId The chosen executor, or "" while nobody is chosen yet. Files picked
 *   before then queue and upload as soon as a persona is picked.
 */
export function useHandOffAttachments(
  personaId: string,
): HandOffAttachmentsState {
  const { getToken } = useAuth();
  const [attachments, setAttachments] = useState<HandOffAttachment[]>([]);
  // Files picked before a persona was chosen. The upload is persona-scoped, so they wait
  // here rather than failing on a path with an empty id.
  const queuedRef = useRef<Map<string, File>>(new Map());

  const token: TokenGetter = useCallback(
    () => getToken(TEMPLATE ? { template: TEMPLATE } : undefined),
    [getToken],
  );

  const patch = useCallback((id: string, next: Partial<HandOffAttachment>) => {
    setAttachments((prev) =>
      prev.map((a) => (a.id === id ? { ...a, ...next } : a)),
    );
  }, []);

  const send = useCallback(
    async (id: string, file: File, owner: string) => {
      try {
        const ref = await uploadTaskAttachment(owner, file, {
          getToken: token,
        });
        patch(id, { state: "ready", ref });
      } catch (e) {
        const detail =
          e instanceof ApiError
            ? `${e.code}${e.detail ? `: ${String(e.detail)}` : ""}`
            : e instanceof Error
              ? e.message
              : String(e);
        patch(id, { state: "error", detail });
      }
    },
    [token, patch],
  );

  // Take everything off the queue BEFORE sending any of it. An upload is async and a
  // re-render can fire the drain effect again while one is still in flight; emptying the
  // queue first is what stops the same file being posted twice.
  const drain = useCallback(
    (owner: string) => {
      const waiting = [...queuedRef.current.entries()];
      queuedRef.current.clear();
      for (const [id, file] of waiting) void send(id, file, owner);
    },
    [send],
  );

  const attach = useCallback(
    (file: File) => {
      const id = crypto.randomUUID();
      setAttachments((prev) => [
        ...prev,
        { id, filename: file.name, state: "uploading" },
      ]);
      if (personaId) {
        void send(id, file, personaId);
        return;
      }
      queuedRef.current.set(id, file);
    },
    [personaId, send],
  );

  // A persona chosen after the file was picked releases the queue. Without this the chip
  // would sit on "uploading" forever, which is exactly the kind of quiet nothing this
  // project treats as a defect.
  useEffect(() => {
    if (personaId) drain(personaId);
  }, [personaId, drain]);

  const remove = useCallback((id: string) => {
    queuedRef.current.delete(id);
    setAttachments((prev) => prev.filter((a) => a.id !== id));
  }, []);

  return {
    attachments,
    refs: attachments.flatMap((a) => (a.ref ? [a.ref] : [])),
    busy: attachments.some((a) => a.state === "uploading"),
    remaining: Math.max(0, MAX_HAND_OFF_ATTACHMENTS - attachments.length),
    attach,
    remove,
  };
}

const ACCEPT = [...IMAGE_MEDIA_TYPES, ...DOCUMENT_EXTENSIONS].join(",");

/** The paperclip button: a hidden file input with a styled label, as the composer does. */
export function HandOffAttachButton({
  onFile,
  remaining = MAX_HAND_OFF_ATTACHMENTS,
  className,
}: {
  onFile: (file: File) => void;
  /** How many more the API will take. At zero the control is visibly unavailable. */
  remaining?: number;
  className?: string;
}) {
  const t = useTranslations("tasks.attach");
  const inputId = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const disabled = remaining <= 0;

  return (
    <>
      <input
        ref={inputRef}
        id={inputId}
        type="file"
        multiple
        accept={ACCEPT}
        className="sr-only"
        aria-label={t("label")}
        disabled={disabled}
        onChange={(e) => {
          // A running count, so selecting six files when two fit takes the first two
          // rather than posting four the API will refuse (the composer does the same).
          for (const file of Array.from(e.target.files ?? []).slice(
            0,
            remaining,
          )) {
            onFile(file);
          }
          // Reset so picking the same file again still fires onChange.
          if (inputRef.current) inputRef.current.value = "";
        }}
      />
      <label
        htmlFor={inputId}
        title={t("label")}
        aria-disabled={disabled}
        data-slot="hand-off-attach"
        className={cn(
          buttonVariants({ variant: "ghost", size: "icon-sm" }),
          "cursor-pointer",
          disabled && "cursor-not-allowed opacity-50",
          className,
        )}
      >
        <Paperclip className="size-4" aria-hidden="true" />
        <span className="sr-only">{t("label")}</span>
      </label>
    </>
  );
}

/** The chip list under the field: what is attached, what is still going up, what failed. */
export function HandOffAttachmentChips({
  attachments,
  onRemove,
}: {
  attachments: readonly HandOffAttachment[];
  onRemove: (id: string) => void;
}) {
  const t = useTranslations("tasks.attach");
  if (attachments.length === 0) return null;
  return (
    <ul
      className="m-0 flex list-none flex-wrap gap-1.5 p-0"
      data-slot="hand-off-attachment-chips"
    >
      {attachments.map((a) => (
        <li
          key={a.id}
          data-slot="hand-off-attachment-chip"
          data-state={a.state}
          title={a.state === "error" ? a.detail : a.filename}
          className={cn(
            "flex items-center gap-1.5 rounded-full border border-border bg-muted/50 py-1 pl-2.5 pr-1 text-xs",
            a.state === "error" && "border-destructive/50 text-destructive",
          )}
        >
          {a.state === "uploading" ? (
            <Loader2 className="size-3 animate-spin" aria-hidden="true" />
          ) : null}
          <span className="max-w-40 truncate">{a.filename}</span>
          {a.state === "error" ? <span>{t("failed")}</span> : null}
          <button
            type="button"
            onClick={() => onRemove(a.id)}
            aria-label={t("remove", { filename: a.filename })}
            className="rounded-full p-0.5 text-muted-foreground hover:bg-muted hover:text-foreground"
          >
            <X className="size-3" aria-hidden="true" />
          </button>
        </li>
      ))}
    </ul>
  );
}
