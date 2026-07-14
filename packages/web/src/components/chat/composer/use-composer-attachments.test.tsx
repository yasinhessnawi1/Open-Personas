import { act, renderHook, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ getToken: () => Promise.resolve("jwt-token") }),
}));

// R9-051 — mock the sonner-backed toast primitive the same way
// use-turn-into-file.test.tsx (R9-050) does, so the loading-toast-start /
// in-place-swap-on-resolve contract can be asserted directly.
const toastFns = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
  info: vi.fn(),
  warning: vi.fn(),
  loading: vi.fn(() => "toast-id-1"),
}));
vi.mock("@/components/patterns/toast", () => ({ toast: toastFns }));

import { NotificationProvider } from "@/components/providers/notification-provider";
import { useComposerAttachments } from "./use-composer-attachments";

const messages = {
  chat: {
    composer: {
      attach: {
        feedback: {
          documentUploading: "Uploading {filename}…",
          documentAttached: "{filename} added to this conversation",
        },
      },
    },
  },
};

function wrapper({ children }: { children: ReactNode }) {
  return (
    <NextIntlClientProvider locale="en" messages={messages}>
      <NotificationProvider>{children}</NotificationProvider>
    </NextIntlClientProvider>
  );
}

function file(name: string, type: string, size = 100): File {
  return new File([new Uint8Array(size)], name, { type });
}

interface FakeXhr {
  open: ReturnType<typeof vi.fn>;
  send: (body: FormData) => void;
  setRequestHeader: ReturnType<typeof vi.fn>;
  abort: ReturnType<typeof vi.fn>;
  getResponseHeader: () => null;
  upload: { onprogress: ((ev: ProgressEvent) => void) | null };
  onload: (() => void) | null;
  onerror: (() => void) | null;
  onabort: (() => void) | null;
  status: number;
  responseText: string;
}

function installFakeXhr(setup: (xhr: FakeXhr) => void): () => void {
  const xhr: FakeXhr = {
    open: vi.fn(),
    send: vi.fn(),
    setRequestHeader: vi.fn(),
    abort: vi.fn(),
    getResponseHeader: () => null,
    upload: { onprogress: null },
    onload: null,
    onerror: null,
    onabort: null,
    status: 0,
    responseText: "",
  };
  setup(xhr);
  const original = globalThis.XMLHttpRequest;
  function FakeXhrCtor(this: unknown): FakeXhr {
    return xhr;
  }
  globalThis.XMLHttpRequest = FakeXhrCtor as unknown as typeof XMLHttpRequest;
  return () => {
    globalThis.XMLHttpRequest = original;
  };
}

const onDocumentAttached = vi.fn();
const onDocumentError = vi.fn();

function defaultOptions(
  overrides: Partial<{ conversationId: string; personaId: string }> = {},
) {
  return {
    conversationId: "conv_1",
    personaId: "persona_a",
    onDocumentAttached,
    onDocumentError,
    ...overrides,
  };
}

describe("useComposerAttachments", () => {
  let restore: () => void;
  beforeEach(() => {
    onDocumentAttached.mockReset();
    onDocumentError.mockReset();
    toastFns.loading.mockClear();
    toastFns.success.mockClear();
    toastFns.error.mockClear();
  });
  afterEach(() => restore?.());

  it("attachImage adds a pending attachment and kicks off upload to success", async () => {
    restore = installFakeXhr((xhr) => {
      xhr.send = vi.fn(() => {
        xhr.status = 201;
        xhr.responseText = JSON.stringify({
          workspace_path: "uploads/photo.png",
          media_type: "image/png",
          size_bytes: 100,
        });
        queueMicrotask(() => xhr.onload?.());
      });
    });

    const { result } = renderHook(
      () => useComposerAttachments(defaultOptions()),
      { wrapper },
    );
    let id = "";
    act(() => {
      id = result.current.attachImage(file("photo.png", "image/png"));
    });
    expect(result.current.attachedImages).toHaveLength(1);
    // Initial state may be "pending" or already "uploading" (the upload
    // kicks off synchronously via void startUpload). Either is correct;
    // the key invariant is that an attachment exists immediately.
    expect(["pending", "uploading"]).toContain(
      result.current.attachedImages[0].state,
    );

    await waitFor(() =>
      expect(result.current.attachedImages[0].state).toBe("success"),
    );
    const att = result.current.attachedImages[0];
    if (att.state !== "success") throw new Error("expected success state");
    expect(att.workspacePath).toBe("uploads/photo.png");
    expect(att.mediaType).toBe("image/png");
    expect(att.id).toBe(id);
  });

  it("attachImage transitions to error on server rejection (D-F3-X-partial-upload-failure-shape)", async () => {
    restore = installFakeXhr((xhr) => {
      xhr.send = vi.fn(() => {
        xhr.status = 422;
        xhr.responseText = JSON.stringify({
          error: "image_validation_error",
          detail: "magic bytes mismatch",
        });
        queueMicrotask(() => xhr.onload?.());
      });
    });

    const { result } = renderHook(
      () => useComposerAttachments(defaultOptions()),
      { wrapper },
    );
    act(() => {
      result.current.attachImage(file("bad.png", "image/png"));
    });
    await waitFor(() =>
      expect(result.current.attachedImages[0].state).toBe("error"),
    );
    const att = result.current.attachedImages[0];
    if (att.state !== "error") throw new Error("expected error state");
    expect(att.reason).toBe("server_rejected");
    expect(att.detail).toContain("image_validation_error");
  });

  it("removeImage drops the attachment from state", async () => {
    restore = installFakeXhr((xhr) => {
      xhr.send = () => {
        xhr.status = 201;
        xhr.responseText = JSON.stringify({
          workspace_path: "uploads/a.png",
          media_type: "image/png",
          size_bytes: 1,
        });
        queueMicrotask(() => xhr.onload?.());
      };
    });

    const { result } = renderHook(
      () => useComposerAttachments(defaultOptions()),
      { wrapper },
    );
    let id = "";
    act(() => {
      id = result.current.attachImage(file("a.png", "image/png"));
    });
    expect(result.current.attachedImages).toHaveLength(1);
    act(() => result.current.removeImage(id));
    expect(result.current.attachedImages).toHaveLength(0);
  });

  it("clearImages empties the state (called after successful send)", async () => {
    restore = installFakeXhr((xhr) => {
      xhr.send = () => {
        xhr.status = 201;
        xhr.responseText = JSON.stringify({
          workspace_path: "uploads/a.png",
          media_type: "image/png",
          size_bytes: 1,
        });
        queueMicrotask(() => xhr.onload?.());
      };
    });

    const { result } = renderHook(
      () => useComposerAttachments(defaultOptions()),
      { wrapper },
    );
    act(() => {
      result.current.attachImage(file("a.png", "image/png"));
      result.current.attachImage(file("b.png", "image/png"));
    });
    await waitFor(() => expect(result.current.attachedImages).toHaveLength(2));
    act(() => result.current.clearImages());
    expect(result.current.attachedImages).toHaveLength(0);
  });

  it("conversation-switch resets attached images (sole dep = conversationId)", async () => {
    restore = installFakeXhr((xhr) => {
      xhr.send = () => {
        xhr.status = 201;
        xhr.responseText = JSON.stringify({
          workspace_path: "uploads/a.png",
          media_type: "image/png",
          size_bytes: 1,
        });
        queueMicrotask(() => xhr.onload?.());
      };
    });

    let convId = "conv_1";
    const { rerender, result } = renderHook(
      () => useComposerAttachments(defaultOptions({ conversationId: convId })),
      { wrapper },
    );
    act(() => {
      result.current.attachImage(file("a.png", "image/png"));
    });
    await waitFor(() => expect(result.current.attachedImages).toHaveLength(1));

    // Conversation switch — message-scoped image state MUST reset.
    convId = "conv_2";
    rerender();
    await waitFor(() => expect(result.current.attachedImages).toHaveLength(0));
  });

  it("uploadDocumentFile threads conversation_id + calls onDocumentAttached on success", async () => {
    restore = installFakeXhr((xhr) => {
      let body: FormData | null = null;
      xhr.send = (b: FormData) => {
        body = b;
        xhr.status = 201;
        xhr.responseText = JSON.stringify({
          doc_ref: "report-1",
          filename: "report.pdf",
          title: "report.pdf",
          format: "pdf",
          workspace_path: "p/c/d/report-1.pdf",
          strategy: "whole_inject",
          token_count: 100,
          page_count: 1,
          sheet_names: null,
          size_bytes: 100,
          images: [],
        });
        // Verify the body carries conversation_id BEFORE firing onload.
        if (body?.get("conversation_id") !== "conv_1") {
          throw new Error("missing conversation_id");
        }
        queueMicrotask(() => xhr.onload?.());
      };
    });

    const { result } = renderHook(
      () => useComposerAttachments(defaultOptions()),
      { wrapper },
    );
    await act(async () => {
      await result.current.uploadDocumentFile(
        file("report.pdf", "application/pdf"),
      );
    });
    expect(onDocumentAttached).toHaveBeenCalledTimes(1);
    expect(onDocumentAttached.mock.calls[0][0].doc_ref).toBe("report-1");
    expect(onDocumentError).not.toHaveBeenCalled();

    // R9-051 — a loading toast opens BEFORE the request resolves (never a
    // dead composer while the POST is in flight), then the SAME toast id
    // is swapped to success in place (R9-050's id-addressable pattern) —
    // never a second, stacked toast.
    expect(toastFns.loading).toHaveBeenCalledTimes(1);
    expect(toastFns.loading).toHaveBeenCalledWith(
      "Uploading report.pdf…",
      undefined,
    );
    expect(toastFns.success).toHaveBeenCalledTimes(1);
    expect(toastFns.success).toHaveBeenCalledWith(
      "report.pdf added to this conversation",
      { id: "toast-id-1" },
    );
  });

  it("uploadDocumentFile calls onDocumentError on server failure and swaps the SAME loading toast to an error", async () => {
    restore = installFakeXhr((xhr) => {
      xhr.send = () => {
        xhr.status = 422;
        xhr.responseText = JSON.stringify({
          error: "corrupt_document",
          detail: "couldn't parse",
        });
        queueMicrotask(() => xhr.onload?.());
      };
    });

    const { result } = renderHook(
      () => useComposerAttachments(defaultOptions()),
      { wrapper },
    );
    await act(async () => {
      await result.current.uploadDocumentFile(file("r.pdf", "application/pdf"));
    });
    expect(onDocumentAttached).not.toHaveBeenCalled();
    expect(onDocumentError).toHaveBeenCalledTimes(1);
    expect(onDocumentError.mock.calls[0][0]).toContain("corrupt_document");

    // R9-051 — failure resolves the SAME toast id to an honest error, never
    // leaves the loading toast hanging and never stacks a second toast.
    expect(toastFns.loading).toHaveBeenCalledTimes(1);
    expect(toastFns.success).not.toHaveBeenCalled();
    expect(toastFns.error).toHaveBeenCalledTimes(1);
    const [, errorOpts] = toastFns.error.mock.calls[0] as [
      string,
      { id?: string } | undefined,
    ];
    expect(errorOpts?.id).toBe("toast-id-1");
  });

  it("retryImage re-runs the upload with the original file", async () => {
    let attemptCount = 0;
    restore = installFakeXhr((xhr) => {
      xhr.send = () => {
        attemptCount += 1;
        if (attemptCount === 1) {
          xhr.status = 503;
          xhr.responseText = JSON.stringify({ error: "service_unavailable" });
        } else {
          xhr.status = 201;
          xhr.responseText = JSON.stringify({
            workspace_path: "uploads/a.png",
            media_type: "image/png",
            size_bytes: 1,
          });
        }
        queueMicrotask(() => xhr.onload?.());
      };
    });

    const { result } = renderHook(
      () => useComposerAttachments(defaultOptions()),
      { wrapper },
    );
    let id = "";
    act(() => {
      id = result.current.attachImage(file("a.png", "image/png"));
    });
    await waitFor(() =>
      expect(result.current.attachedImages[0].state).toBe("error"),
    );
    act(() => result.current.retryImage(id));
    await waitFor(() =>
      expect(result.current.attachedImages[0].state).toBe("success"),
    );
    expect(attemptCount).toBe(2);
  });
});
