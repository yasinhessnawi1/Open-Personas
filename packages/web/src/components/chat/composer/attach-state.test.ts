import { describe, expect, it } from "vitest";
import {
  MAX_IMAGES_PER_MESSAGE,
  MAX_UPLOAD_SIZE_BYTES,
} from "@/lib/api/limits";
import {
  type Attachment,
  attachmentsBlockSend,
  validateBeforeUpload,
} from "./attach-state";

function file(name: string, type: string, size = 100): File {
  return new File([new Uint8Array(size)], name, { type });
}

describe("validateBeforeUpload — D-F3-3 client-side pre-validation", () => {
  describe("happy paths", () => {
    it("accepts an image under the per-message cap", () => {
      const result = validateBeforeUpload(file("a.png", "image/png"), 0);
      expect(result).toEqual({ ok: true, kind: "image" });
    });

    it("accepts a PDF with declared content-type", () => {
      const result = validateBeforeUpload(file("r.pdf", "application/pdf"), 0);
      expect(result).toEqual({ ok: true, kind: "document" });
    });

    it("accepts a code file via extension when content-type is empty", () => {
      const result = validateBeforeUpload(file("main.py", ""), 0);
      expect(result).toEqual({ ok: true, kind: "document" });
    });

    it("accepts an image at the cap-of-3 (4 max — currentCount=3 still permits)", () => {
      const result = validateBeforeUpload(file("a.png", "image/png"), 3);
      expect(result.ok).toBe(true);
    });
  });

  describe("rejections", () => {
    it("rejects empty files with reason=empty_file", () => {
      const result = validateBeforeUpload(file("empty.png", "image/png", 0), 0);
      expect(result).toMatchObject({ ok: false, reason: "empty_file" });
    });

    it("rejects files over MAX_UPLOAD_SIZE_BYTES with reason=oversize", () => {
      const result = validateBeforeUpload(
        file("big.png", "image/png", MAX_UPLOAD_SIZE_BYTES + 1),
        0,
      );
      expect(result).toMatchObject({ ok: false, reason: "oversize" });
      if (!result.ok) {
        expect(result.params.limit).toBe("20.0 MB");
      }
    });

    it("rejects the 5th image when 4 are already attached (per-message cap)", () => {
      const result = validateBeforeUpload(
        file("a.png", "image/png"),
        MAX_IMAGES_PER_MESSAGE,
      );
      expect(result).toMatchObject({
        ok: false,
        reason: "per_message_image_cap",
      });
      if (!result.ok) {
        expect(result.params.cap).toBe(MAX_IMAGES_PER_MESSAGE);
      }
    });

    it("rejects unsupported formats with reason=unsupported_format", () => {
      const result = validateBeforeUpload(file("video.mp4", "video/mp4"), 0);
      expect(result).toMatchObject({ ok: false, reason: "unsupported_format" });
    });

    it("rejects image/svg+xml (not in the 4-literal union)", () => {
      const result = validateBeforeUpload(file("d.svg", "image/svg+xml"), 0);
      // SVG is image/* but not in the 4-literal union; the dispatcher's
      // image branch checks `isImageMediaType` strictly. The fallback path
      // checks extension — `.svg` is not in DOCUMENT_EXTENSIONS either, so
      // it rejects.
      expect(result).toMatchObject({ ok: false, reason: "unsupported_format" });
    });
  });
});

describe("attachmentsBlockSend — D-F3-X-partial-upload-failure-shape", () => {
  function imgAttachment(
    state: "pending" | "uploading" | "success" | "error",
  ): Attachment {
    const base = {
      kind: "image" as const,
      id: "i1",
      file: file("a.png", "image/png"),
    };
    switch (state) {
      case "pending":
        return { ...base, state: "pending" };
      case "uploading":
        return { ...base, state: "uploading", progress: 0.5 };
      case "success":
        return {
          ...base,
          state: "success",
          workspacePath: "uploads/a.png",
          mediaType: "image/png",
        };
      case "error":
        return {
          ...base,
          state: "error",
          reason: "server_rejected",
          detail: "magic bytes mismatch",
        };
    }
  }

  it("blocks send when any image is uploading", () => {
    expect(attachmentsBlockSend([imgAttachment("uploading")])).toBe(true);
  });

  it("blocks send when any image is in error state", () => {
    // Fail-loud over silent-drop: failed attachments require explicit
    // remove or retry before send proceeds.
    expect(attachmentsBlockSend([imgAttachment("error")])).toBe(true);
  });

  it("does NOT block send when all images are success", () => {
    expect(
      attachmentsBlockSend([
        imgAttachment("success"),
        imgAttachment("success"),
      ]),
    ).toBe(false);
  });

  it("does NOT block send when attachments are empty (text-only message)", () => {
    expect(attachmentsBlockSend([])).toBe(false);
  });

  it("does NOT block send for pending images (no in-flight upload yet)", () => {
    // Pending is the state between selection and the upload kicking off;
    // brief enough that we don't block, and the composer triggers the
    // upload synchronously after pre-validation.
    expect(attachmentsBlockSend([imgAttachment("pending")])).toBe(false);
  });

  it("ignores document attachments for send-blocking", () => {
    // Documents are conversation-scoped, not message-scoped. A failed
    // document upload is removed independently — send proceeds with the
    // remaining documents (and any successful images).
    const docError: Attachment = {
      kind: "document",
      id: "d1",
      state: "error",
      file: file("r.pdf", "application/pdf"),
      reason: "server_rejected",
      detail: "corrupt",
    };
    expect(attachmentsBlockSend([docError])).toBe(false);
  });
});
