/**
 * F3 — composer attach-state types + client-side pre-validation (T05).
 *
 * Pure functions + frozen types; no React. Owned by the composer
 * (`<ChatWindow>` extension at T19) and consumed by the attach control
 * (T07), the image preview (T09), and the document panel (T12).
 *
 * Client-side pre-validation per D-F3-3 — instant feedback for size /
 * format / per-message cap. The server (Spec 13/14) remains
 * authoritative; the client only short-circuits the obviously-doomed
 * uploads. Validation reasons are typed enum so F2's error voice (T16)
 * can map them to i18n keys without string parsing.
 */

import {
  isDocumentFilename,
  isImageMediaType,
  MAX_IMAGES_PER_MESSAGE,
  MAX_UPLOAD_SIZE_BYTES,
} from "@/lib/api/limits";

/** Lifecycle of a single attached file. */
export type UploadStateKind = "pending" | "uploading" | "success" | "error";

/**
 * Attached file's complete state slice. Discriminated union by `state`
 * so consumers can pattern-match without optional chaining.
 *
 * `id` is a client-generated UUID (the composer's local handle, distinct
 * from any server-side reference); `file` is the original browser File
 * (retained for retry on failure per D-F3-X-partial-upload-failure-shape).
 */
export type ImageAttachment =
  | {
      kind: "image";
      id: string;
      state: "pending";
      file: File;
    }
  | {
      kind: "image";
      id: string;
      state: "uploading";
      file: File;
      /** Fraction in [0, 1]; null when upload progress is indeterminate (D-F3-4). */
      progress: number | null;
    }
  | {
      kind: "image";
      id: string;
      state: "success";
      file: File;
      /** Workspace reference returned by Spec 13's upload service. */
      workspacePath: string;
      mediaType: string;
    }
  | {
      kind: "image";
      id: string;
      state: "error";
      file: File;
      /** Validation reason from Spec 13's structured detail (e.g. magic_bytes_mismatch). */
      reason: ValidationReason | "server_rejected";
      /** Server-provided message for the user (i18n-friendly when reason is known). */
      detail: string;
    };

/** Document attach state — same lifecycle, distinct discriminator. */
export type DocumentAttachment =
  | {
      kind: "document";
      id: string;
      state: "pending";
      file: File;
    }
  | {
      kind: "document";
      id: string;
      state: "uploading";
      file: File;
      progress: number | null;
    }
  | {
      kind: "document";
      id: string;
      state: "success";
      file: File;
      /** DocumentRef from Spec 14's upload service. */
      docRef: string;
    }
  | {
      kind: "document";
      id: string;
      state: "error";
      file: File;
      reason: ValidationReason | "server_rejected";
      detail: string;
    };

export type Attachment = ImageAttachment | DocumentAttachment;

/**
 * Why a pre-validation rejected a file. Maps 1:1 to i18n keys so F2's
 * error voice surfaces honestly ("not just upload failed"). The
 * `server_rejected` case is reserved for non-client-detectable failures
 * (decompression-bomb, magic-byte mismatch, etc. — Spec 13/14 return
 * structured details).
 */
export type ValidationReason =
  | "unsupported_format"
  | "oversize"
  | "per_message_image_cap"
  | "empty_file"
  // Deployment/context refusals raised by the attach control itself, not by
  // `validateBeforeUpload`. They ride the same rejection channel so every
  // refusal reads from the message catalogue.
  | "image_attach_disabled"
  | "documents_need_conversation";

/** ICU values for the `chat.composer.validation.<reason>` message. Structured,
 * never prose — the copy lives in the message catalogue, not in this module. */
export type ValidationParams = Record<string, string | number>;

export type ValidationResult =
  | { ok: true; kind: "image" | "document" }
  | { ok: false; reason: ValidationReason; params: ValidationParams };

/**
 * Validate a single browser `File` before upload. Returns the routing
 * decision (image vs document) on success; a typed rejection otherwise.
 *
 * @param file                  The file the user selected / dropped / pasted.
 * @param currentImageCount     How many images are already attached to the
 *                              composer (so we can reject the 5th image when
 *                              the cap is 4). Pass 0 for documents (Spec 14
 *                              has no per-conversation count cap at v0.1).
 *
 * Pure; safe to call in render. Server remains authoritative (D-F3-3).
 */
export function validateBeforeUpload(
  file: File,
  currentImageCount: number,
): ValidationResult {
  if (file.size === 0) {
    return {
      ok: false,
      reason: "empty_file",
      params: { filename: file.name },
    };
  }

  if (file.size > MAX_UPLOAD_SIZE_BYTES) {
    return {
      ok: false,
      reason: "oversize",
      params: {
        filename: file.name,
        limit: formatBytes(MAX_UPLOAD_SIZE_BYTES),
      },
    };
  }

  // Content-type dispatch mirrors routes/uploads.py:128 — image first,
  // then document, then reject.
  if (isImageMediaType(file.type)) {
    if (currentImageCount >= MAX_IMAGES_PER_MESSAGE) {
      return {
        ok: false,
        reason: "per_message_image_cap",
        params: { cap: MAX_IMAGES_PER_MESSAGE },
      };
    }
    return { ok: true, kind: "image" };
  }

  // Fall back on the extension when content-type is missing or generic
  // (common from drag sources). Same `_is_document_filename` logic as
  // the API dispatcher.
  if (isDocumentFilename(file.name) || file.type === "application/pdf") {
    return { ok: true, kind: "document" };
  }

  return {
    ok: false,
    reason: "unsupported_format",
    params: { filename: file.name },
  };
}

/** Pretty-print byte counts ("20.0 MB"). Used by validation messages. */
function formatBytes(n: number): string {
  const mb = n / (1024 * 1024);
  return `${mb.toFixed(1)} MB`;
}

/**
 * Whether send should be blocked by attachments. Per D-F3-X-partial-
 * upload-failure-shape, any attached image in `uploading` OR `error`
 * blocks send — the user must retry or remove before the message goes.
 * Document attachments do NOT block (they're conversation-scoped; failed
 * documents are removed independently of send).
 */
export function attachmentsBlockSend(attachments: Attachment[]): boolean {
  return attachments.some(
    (a) =>
      a.kind === "image" && (a.state === "uploading" || a.state === "error"),
  );
}
