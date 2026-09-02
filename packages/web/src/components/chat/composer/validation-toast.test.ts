import { describe, expect, it, vi } from "vitest";
import messages from "@/i18n/messages/en.json";
import { surfaceValidationFailure } from "./validation-toast";

/**
 * F3 T16 + T17 — a rejection is a CATALOGUE message, never English built in
 * the validator. The stub resolves `chat.composer.<key>` out of en.json and
 * fills the ICU placeholders, which is exactly what next-intl does at runtime.
 */
function translator() {
  return ((key: string, values?: Record<string, string | number>) => {
    const node = key
      .split(".")
      .reduce<unknown>(
        (acc, part) => (acc as Record<string, unknown>)?.[part],
        messages.chat.composer as unknown,
      );
    return String(node).replace(/\{(\w+)\}/g, (_, name) =>
      String(values?.[name] ?? ""),
    );
    // biome-ignore lint/suspicious/noExplicitAny: test stub for next-intl's t
  }) as any;
}

describe("surfaceValidationFailure — F3 T16 + T17", () => {
  it("emits a single error toast built from the catalogue", () => {
    const toast = { error: vi.fn() };
    surfaceValidationFailure(
      "oversize",
      { filename: "big.png", limit: "20.0 MB" },
      toast,
      translator(),
    );
    expect(toast.error).toHaveBeenCalledTimes(1);
    expect(toast.error.mock.calls[0][0]).toContain("20.0 MB");
    expect(toast.error.mock.calls[0][0]).toContain("big.png");
  });

  it("surfaces the per-message cap with the real cap value (T17)", () => {
    const toast = { error: vi.fn() };
    surfaceValidationFailure(
      "per_message_image_cap",
      { cap: 4 },
      toast,
      translator(),
    );
    expect(toast.error.mock.calls[0][0]).toContain("4 images");
  });

  it("surfaces unsupported-format details (T16 — F2 honest voice)", () => {
    const toast = { error: vi.fn() };
    surfaceValidationFailure(
      "unsupported_format",
      { filename: "video.mp4" },
      toast,
      translator(),
    );
    expect(toast.error.mock.calls[0][0]).toContain("video.mp4");
    // F2 voice: NOT "upload failed" — the user sees WHY.
    expect(toast.error.mock.calls[0][0]).not.toBe("upload failed");
  });

  it("surfaces empty-file rejection", () => {
    const toast = { error: vi.fn() };
    surfaceValidationFailure(
      "empty_file",
      { filename: "empty.png" },
      toast,
      translator(),
    );
    expect(toast.error.mock.calls[0][0]).toContain("empty");
  });

  it("maps the deployment refusals onto the attach tooltip copy", () => {
    const toast = { error: vi.fn() };
    surfaceValidationFailure("image_attach_disabled", {}, toast, translator());
    expect(toast.error.mock.calls[0][0]).toBe(
      messages.chat.composer.attach.imageDisabled,
    );
    surfaceValidationFailure(
      "documents_need_conversation",
      {},
      toast,
      translator(),
    );
    expect(toast.error.mock.calls[1][0]).toBe(
      messages.chat.composer.attach.openConversationFirst,
    );
  });
});
