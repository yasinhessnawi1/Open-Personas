"use client";

import { Paperclip } from "lucide-react";
import { useTranslations } from "next-intl";
import { useId, useRef } from "react";
import { buttonVariants } from "@/components/ui/button";
import {
  DOCUMENT_EXTENSIONS,
  IMAGE_MEDIA_TYPES,
  MAX_IMAGES_PER_MESSAGE,
} from "@/lib/api/limits";
import { cn } from "@/lib/utils";
import {
  type ValidationParams,
  type ValidationReason,
  validateBeforeUpload,
} from "./attach-state";

/**
 * F3 — composer attach control (T07).
 *
 * Composes F2's `<Button size="icon" variant="ghost">` + a hidden
 * `<input type="file">`. Clicking the button opens the file picker;
 * each selected file is pre-validated (T05) and routed to either
 * `onImageFile` or `onDocumentFile` per content-type dispatch
 * (D-F3-1). Rejected files surface via `onReject`.
 *
 * Disabled when `imageAttachDisabled` is true (D-F3-X-no-vision-surface-
 * shape (a) — `capabilities.vision === false`) AND no document attach
 * is possible either (`documentsDisabled`, e.g. on persona-detail per
 * D-F3-X-document-attach-conversation-binding). When only ONE side is
 * disabled, the button stays enabled — the input's `accept` attribute
 * narrows to only the available format class.
 *
 * Strings + ARIA labels go through `t()` per the F2 i18n convention
 * (and the T20 a11y verification asserts ARIA labels are i18n keys,
 * not raw English).
 */
export interface ComposerAttachControlProps {
  /** Image branch sink — called per accepted image file. */
  onImageFile: (file: File) => void;
  /** Document branch sink — called per accepted document file. */
  onDocumentFile: (file: File) => void;
  /** Rejection sink — typed reason + i18n-friendly detail (T16 toasts via F2 error voice). */
  onReject: (reason: ValidationReason, params: ValidationParams) => void;
  /** Current attached image count for the per-message cap check. */
  currentImageCount: number;
  /** True when D-F3-X-no-vision-surface-shape (a) disables image attach. */
  imageAttachDisabled?: boolean;
  /** True when not inside a conversation context (D-F3-X-document-attach-conversation-binding). */
  documentsDisabled?: boolean;
}

/**
 * Build the `accept` attribute from the two switches. Empty allow-list
 * collapses to disabled control (parent unwraps).
 */
function buildAcceptAttribute(
  imageAttachDisabled: boolean,
  documentsDisabled: boolean,
): string {
  const parts: string[] = [];
  if (!imageAttachDisabled) parts.push(...IMAGE_MEDIA_TYPES);
  if (!documentsDisabled) parts.push(...DOCUMENT_EXTENSIONS);
  return parts.join(",");
}

export function ComposerAttachControl({
  onImageFile,
  onDocumentFile,
  onReject,
  currentImageCount,
  imageAttachDisabled = false,
  documentsDisabled = false,
}: ComposerAttachControlProps) {
  const t = useTranslations("chat.composer");
  const inputId = useId();
  const inputRef = useRef<HTMLInputElement>(null);

  // Whole-control disabled only when BOTH branches are off. Otherwise
  // the user can still attach the available class (e.g. on persona detail
  // images are valid but documents aren't).
  const fullyDisabled = imageAttachDisabled && documentsDisabled;
  const accept = buildAcceptAttribute(imageAttachDisabled, documentsDisabled);

  // Tooltip explains *why* it's disabled — Spec 13 / Spec 14 honest copy
  // per D-F3-X-no-vision-tooltip-copy (deployment-derived) and
  // D-F3-X-document-attach-conversation-binding.
  const tooltip = imageAttachDisabled
    ? t("attach.imageDisabled")
    : documentsDisabled
      ? t("attach.openConversationFirst")
      : t("attach.label");

  function handleFiles(fileList: FileList | null) {
    if (!fileList || fileList.length === 0) return;
    // Track running image-count so multi-file selection respects the cap
    // (don't accept image #4 + #5 in the same selection).
    let pendingImageCount = currentImageCount;
    for (const file of Array.from(fileList)) {
      const result = validateBeforeUpload(file, pendingImageCount);
      if (!result.ok) {
        onReject(result.reason, result.params);
        continue;
      }
      if (result.kind === "image") {
        if (imageAttachDisabled) {
          onReject("image_attach_disabled", {});
          continue;
        }
        pendingImageCount += 1;
        onImageFile(file);
      } else {
        if (documentsDisabled) {
          onReject("documents_need_conversation", {});
          continue;
        }
        onDocumentFile(file);
      }
    }
    // Reset the input so selecting the same file again triggers `onChange`.
    if (inputRef.current) inputRef.current.value = "";
  }

  return (
    <>
      <input
        ref={inputRef}
        id={inputId}
        type="file"
        multiple
        accept={accept}
        className="sr-only"
        aria-label={tooltip}
        onChange={(e) => handleFiles(e.target.files)}
        disabled={fullyDisabled}
      />
      <label
        htmlFor={inputId}
        title={tooltip}
        aria-disabled={fullyDisabled}
        data-image-cap={MAX_IMAGES_PER_MESSAGE}
        className={cn(
          buttonVariants({ variant: "ghost", size: "icon" }),
          "cursor-pointer",
          fullyDisabled && "cursor-not-allowed opacity-50",
        )}
      >
        <Paperclip className="size-4" aria-hidden />
        <span className="sr-only">{tooltip}</span>
      </label>
    </>
  );
}
