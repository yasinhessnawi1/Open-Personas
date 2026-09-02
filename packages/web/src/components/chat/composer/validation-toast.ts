/**
 * F3 (T16 + T17) — typed mapping from `ValidationReason` to F2 toast
 * messages via the i18n table.
 *
 * `validateBeforeUpload` (T05) returns a typed reason enum on rejection;
 * this module maps each enum value to the corresponding i18n key the
 * toast surfaces. Centralised here so T19's ChatWindow wiring stays
 * thin (one function call per rejection) and so T20's a11y verification
 * can grep for the keys directly.
 *
 * T18 — the scanned-PDF UX cue — does NOT go through here; it's a
 * static inline tooltip on `<DocumentChip>` (already shipped via
 * the `documents.scannedCue` i18n key + `strategy === "vision_handoff"`
 * detection in document-chip.tsx).
 */

import type { useTranslations } from "next-intl";
import type { ValidationParams, ValidationReason } from "./attach-state";

export interface ToastSink {
  error: (message: string) => void;
}

/** Reason → message key, relative to the `chat.composer` namespace. The two
 * deployment refusals reuse the tooltip copy they already share. */
const MESSAGE_KEY: Record<ValidationReason, string> = {
  empty_file: "validation.empty_file",
  oversize: "validation.oversize",
  per_message_image_cap: "validation.per_message_image_cap",
  unsupported_format: "validation.unsupported_format",
  image_attach_disabled: "attach.imageDisabled",
  documents_need_conversation: "attach.openConversationFirst",
};

/**
 * Surface a typed validation failure as a toast.
 *
 * @param reason  The typed enum from validateBeforeUpload (T05).
 * @param params  ICU values (filename / cap / limit) for the message.
 * @param toast   sonner `toast` from useToast() (T19 passes it down).
 * @param t       next-intl translator for `chat.composer.validation.*` keys.
 *
 * The whole sentence comes from the catalogue: `validation.<reason>` with the
 * structured params interpolated, so the rejection translates with the locale
 * instead of shipping English out of the validator.
 */
export function surfaceValidationFailure(
  reason: ValidationReason,
  params: ValidationParams,
  toast: ToastSink,
  t: ReturnType<typeof useTranslations>,
): void {
  // Single error toast per rejection. F2's `<ToastProvider>` is mounted
  // once in <AppShell>; toasts surface in the top-right with status colour.
  toast.error(t(MESSAGE_KEY[reason], params));
}
