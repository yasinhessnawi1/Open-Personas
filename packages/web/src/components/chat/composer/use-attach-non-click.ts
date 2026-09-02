"use client";

import { type RefObject, useEffect, useState } from "react";

/**
 * F3 — composer drag-and-drop + paste handlers (T08).
 *
 * Both hooks are **desktop-only enhancements** per X-F3-3. Mobile browsers
 * don't dispatch drag events on touch, and there's no clipboard-image
 * equivalent on mobile keyboards — the attach button (T07) remains the
 * universal fallback. The hooks register listeners imperatively (no
 * passive-touch interference) and gracefully no-op when events don't fire.
 *
 * Both pass each accepted file to the same callback the attach button
 * uses (`onFile`), so the composer routes files through a single pre-
 * validation path (`validateBeforeUpload` at T05).
 */

export interface UseDragTargetOptions {
  /** Called per file dropped onto the target. */
  onFiles: (files: File[]) => void;
  /** Called when the user drags a folder / a remote URL / anything we can't accept. */
  onReject: (detail: string) => void;
  /** The already-translated message for a folder drop — the copy lives in the
   * caller's message catalogue (`chat.composer.attach.folderRejected`), never
   * as English in this hook. */
  folderRejectDetail: string;
  /** Set to true to disable the handler entirely (e.g. on touch UA or persona detail). */
  disabled?: boolean;
}

/**
 * Subscribe to drag-and-drop events on the given container ref.
 *
 * Edge cases handled (X-F3-3):
 * - **Folder drop:** `DataTransferItem.webkitGetAsEntry()` returns a
 *   `FileSystemDirectoryEntry`; rejected with a clear message.
 * - **Remote URL drag (from another tab):** `DataTransfer.items[i].kind`
 *   is "string" not "file"; rejected silently (no toast — the user
 *   didn't intend to attach a URL).
 * - **Multi-file drag:** every file in the transfer goes through onFiles
 *   in order; per-file routing happens at the composer level.
 *
 * Returns `isOver` state for the visual cue (T19 wires a `<FadeTransition>`
 * overlay; on touch the value stays `false` because events never fire).
 */
export function useDragTarget(
  ref: RefObject<HTMLElement | null>,
  options: UseDragTargetOptions,
): boolean {
  const { onFiles, onReject, folderRejectDetail, disabled } = options;
  const [isOver, setIsOver] = useState(false);

  useEffect(() => {
    if (disabled) return;
    const el = ref.current;
    if (!el) return;

    function onDragOver(e: DragEvent) {
      // Only react when files are actually present in the drag — ignore
      // text-only drags between page elements.
      if (e.dataTransfer?.types.includes("Files")) {
        e.preventDefault();
        setIsOver(true);
      }
    }

    function onDragLeave(e: DragEvent) {
      // dragleave fires when crossing child boundaries; only clear when
      // leaving the container entirely.
      if (e.target === el) setIsOver(false);
    }

    function onDrop(e: DragEvent) {
      e.preventDefault();
      setIsOver(false);
      const items = Array.from(e.dataTransfer?.items ?? []);
      const files: File[] = [];
      for (const item of items) {
        if (item.kind !== "file") {
          // Remote URL drag, string drag, etc. — silently skip.
          continue;
        }
        const entry = item.webkitGetAsEntry?.();
        if (entry && !entry.isFile) {
          onReject(folderRejectDetail);
          continue;
        }
        const f = item.getAsFile();
        if (f) files.push(f);
      }
      if (files.length > 0) onFiles(files);
    }

    el.addEventListener("dragover", onDragOver);
    el.addEventListener("dragleave", onDragLeave);
    el.addEventListener("drop", onDrop);
    return () => {
      el.removeEventListener("dragover", onDragOver);
      el.removeEventListener("dragleave", onDragLeave);
      el.removeEventListener("drop", onDrop);
    };
  }, [ref, onFiles, onReject, disabled, folderRejectDetail]);

  return isOver;
}

export interface UsePasteImageOptions {
  /** Called per pasted image file (clipboardData.items). */
  onFile: (file: File) => void;
  /** Set to true to disable the handler entirely. */
  disabled?: boolean;
}

/**
 * Subscribe to paste events on the given input ref — accepts IMAGES ONLY.
 *
 * Paste-to-upload is a desktop-only convenience for screenshots /
 * copied images. Pasting text still falls through to the textarea
 * (we never call preventDefault on a non-image paste). Pasting a
 * document does nothing — the document attach path is button-only.
 */
export function usePasteImage(
  ref: RefObject<HTMLElement | null>,
  options: UsePasteImageOptions,
): void {
  const { onFile, disabled } = options;

  useEffect(() => {
    if (disabled) return;
    const el = ref.current;
    if (!el) return;

    function onPaste(e: ClipboardEvent) {
      const items = Array.from(e.clipboardData?.items ?? []);
      for (const item of items) {
        if (item.kind === "file" && item.type.startsWith("image/")) {
          const f = item.getAsFile();
          if (f) {
            // preventDefault so the image doesn't also paste into the
            // textarea as a base64 data URI string.
            e.preventDefault();
            onFile(f);
            return;
          }
        }
      }
    }

    el.addEventListener("paste", onPaste as EventListener);
    return () => {
      el.removeEventListener("paste", onPaste as EventListener);
    };
  }, [ref, onFile, disabled]);
}
