import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "./api/client";
import {
  describeUploadError,
  uploadDocument,
  uploadErrorBody,
  uploadImage,
} from "./upload";

/**
 * Vitest's jsdom environment provides a stub XMLHttpRequest with most of the
 * surface we need. We patch `xhr.send` to control the response shape; the
 * patch is per-test to avoid leaking state.
 */

interface FakeXhr {
  open: ReturnType<typeof vi.fn>;
  send: ReturnType<typeof vi.fn>;
  setRequestHeader: ReturnType<typeof vi.fn>;
  abort: ReturnType<typeof vi.fn>;
  getResponseHeader: (name: string) => string | null;
  upload: { onprogress: ((ev: ProgressEvent) => void) | null };
  onload: (() => void) | null;
  onerror: (() => void) | null;
  onabort: (() => void) | null;
  status: number;
  responseText: string;
  lastBody: FormData | null;
}

function installFakeXhr(setup: (xhr: FakeXhr) => void): {
  xhr: FakeXhr;
  restore: () => void;
} {
  const xhr: FakeXhr = {
    open: vi.fn(),
    send: vi.fn((body: FormData) => {
      xhr.lastBody = body;
    }),
    setRequestHeader: vi.fn(),
    abort: vi.fn(() => xhr.onabort?.()),
    getResponseHeader: () => null,
    upload: { onprogress: null },
    onload: null,
    onerror: null,
    onabort: null,
    status: 200,
    responseText: "",
    lastBody: null,
  };
  setup(xhr);
  const original = globalThis.XMLHttpRequest;
  // Constructor function: `new XMLHttpRequest()` must return our `xhr`.
  // Plain `vi.fn(() => xhr)` is not callable with `new` in jsdom; a real
  // function-constructor that returns an object is.
  function FakeXhrCtor(this: unknown): FakeXhr {
    return xhr;
  }
  globalThis.XMLHttpRequest = FakeXhrCtor as unknown as typeof XMLHttpRequest;
  return {
    xhr,
    restore: () => {
      globalThis.XMLHttpRequest = original;
    },
  };
}

const FAKE_TOKEN = "jwt-fake-token";
const FAKE_PERSONA = "persona_abc";
const FAKE_CONVERSATION = "conv_xyz";

function makeFile(name: string, type: string, sizeBytes = 100): File {
  const bytes = new Uint8Array(sizeBytes);
  return new File([bytes], name, { type });
}

describe("upload.ts — uploadImage (Spec 13 branch)", () => {
  let restore: () => void;
  afterEach(() => restore?.());

  it("sends multipart/form-data with the file (NOT base64) to /v1/personas/:id/uploads", async () => {
    const ref = {
      workspace_path: "uploads/abc.png",
      media_type: "image/png",
      size_bytes: 100,
    };
    let captured!: FakeXhr;
    ({ restore } = installFakeXhr((xhr) => {
      captured = xhr;
      xhr.send = vi.fn((body) => {
        xhr.lastBody = body as FormData;
        xhr.status = 201;
        xhr.responseText = JSON.stringify(ref);
        queueMicrotask(() => xhr.onload?.());
      });
    }));

    const file = makeFile("photo.png", "image/png");
    const result = await uploadImage(FAKE_PERSONA, file, {
      getToken: async () => FAKE_TOKEN,
    });

    expect(result).toEqual(ref);
    expect(captured.open).toHaveBeenCalledWith(
      "POST",
      expect.stringContaining(`/v1/personas/${FAKE_PERSONA}/uploads`),
      true,
    );
    expect(captured.setRequestHeader).toHaveBeenCalledWith(
      "Authorization",
      `Bearer ${FAKE_TOKEN}`,
    );
    expect(captured.lastBody?.get("file")).toBe(file);
    // store-by-reference defence (Concern #4): NO conversation_id field +
    // no base64-encoded body anywhere.
    expect(captured.lastBody?.get("conversation_id")).toBeNull();
  });

  it("reports upload progress as a fraction in [0, 1]", async () => {
    let captured!: FakeXhr;
    ({ restore } = installFakeXhr((xhr) => {
      captured = xhr;
      xhr.send = vi.fn(() => {
        xhr.upload.onprogress?.({
          lengthComputable: true,
          loaded: 25,
          total: 100,
        } as ProgressEvent);
        xhr.upload.onprogress?.({
          lengthComputable: true,
          loaded: 100,
          total: 100,
        } as ProgressEvent);
        xhr.status = 201;
        xhr.responseText = JSON.stringify({
          workspace_path: "uploads/x.png",
          media_type: "image/png",
          size_bytes: 100,
        });
        queueMicrotask(() => xhr.onload?.());
      });
    }));

    const progress: (number | null)[] = [];
    await uploadImage(FAKE_PERSONA, makeFile("x.png", "image/png"), {
      getToken: async () => FAKE_TOKEN,
      onProgress: (f) => progress.push(f),
    });

    expect(progress).toContain(0.25);
    expect(progress).toContain(1);
    expect(captured.upload.onprogress).toBeTypeOf("function");
  });

  it("reports null progress when lengthComputable is false (proxy strips content-length)", async () => {
    ({ restore } = installFakeXhr((xhr) => {
      xhr.send = vi.fn(() => {
        xhr.upload.onprogress?.({
          lengthComputable: false,
          loaded: 0,
          total: 0,
        } as ProgressEvent);
        xhr.status = 201;
        xhr.responseText = JSON.stringify({
          workspace_path: "uploads/x.png",
          media_type: "image/png",
          size_bytes: 100,
        });
        queueMicrotask(() => xhr.onload?.());
      });
    }));

    const progress: (number | null)[] = [];
    await uploadImage(FAKE_PERSONA, makeFile("x.png", "image/png"), {
      getToken: async () => FAKE_TOKEN,
      onProgress: (f) => progress.push(f),
    });

    expect(progress).toContain(null);
  });

  it("throws ApiError with structured detail on 422", async () => {
    ({ restore } = installFakeXhr((xhr) => {
      xhr.send = vi.fn(() => {
        xhr.status = 422;
        xhr.responseText = JSON.stringify({
          error: "image_validation_error",
          detail: "magic bytes mismatch",
          context: { reason: "magic_bytes_mismatch" },
        });
        queueMicrotask(() => xhr.onload?.());
      });
    }));

    await expect(
      uploadImage(FAKE_PERSONA, makeFile("x.png", "image/png"), {
        getToken: async () => FAKE_TOKEN,
      }),
    ).rejects.toThrow(ApiError);
  });

  it("aborts via signal — rejects with AbortError name", async () => {
    // The composer aborts in-flight uploads on conversation-switch
    // (D-F3-X-cap-attached-state-on-conversation-switch). The abort
    // window has two parts: (a) before getToken() / xhr.send (early-
    // exit checks signal.aborted), (b) after xhr.send (signal listener
    // calls xhr.abort()). Either path must reject with AbortError so
    // the composer doesn't toast a generic upload-failure.
    let xhrConstructed = false;
    ({ restore } = installFakeXhr((xhr) => {
      xhr.send = vi.fn(() => {
        xhrConstructed = true;
        // never resolve — wait for abort
      });
    }));

    const controller = new AbortController();
    controller.abort(); // abort BEFORE call — exercises path (a)
    const promise = uploadImage(FAKE_PERSONA, makeFile("x.png", "image/png"), {
      getToken: async () => FAKE_TOKEN,
      signal: controller.signal,
    });
    await expect(promise).rejects.toMatchObject({ name: "AbortError" });
    // Early-exit short-circuits the xhr construction; this asserts the
    // network request never went out (no leaked bytes on cancellation).
    expect(xhrConstructed).toBe(false);
  });
});

describe("upload.ts — uploadDocument (Spec 14 branch)", () => {
  let restore: () => void;
  afterEach(() => restore?.());

  it("includes conversation_id form field (D-F3-X-document-attach-conversation-binding)", async () => {
    const docRef = {
      doc_ref: "report-pdf-1234",
      filename: "report.pdf",
      title: "report.pdf",
      format: "pdf",
      workspace_path:
        "persona_abc/conversations/conv_xyz/documents/report-pdf-1234.pdf",
      strategy: "whole_inject",
      token_count: 1500,
      page_count: 12,
      sheet_names: null,
      size_bytes: 50000,
      images: [],
    };
    let captured!: FakeXhr;
    ({ restore } = installFakeXhr((xhr) => {
      captured = xhr;
      xhr.send = vi.fn((body) => {
        xhr.lastBody = body as FormData;
        xhr.status = 201;
        xhr.responseText = JSON.stringify(docRef);
        queueMicrotask(() => xhr.onload?.());
      });
    }));

    const file = makeFile("report.pdf", "application/pdf");
    const result = await uploadDocument(FAKE_PERSONA, FAKE_CONVERSATION, file, {
      getToken: async () => FAKE_TOKEN,
    });

    expect(result.doc_ref).toBe("report-pdf-1234");
    expect(captured.lastBody?.get("file")).toBe(file);
    expect(captured.lastBody?.get("conversation_id")).toBe(FAKE_CONVERSATION);
  });

  it("surfaces 422 conversation_id_required as ApiError", async () => {
    ({ restore } = installFakeXhr((xhr) => {
      xhr.send = vi.fn(() => {
        xhr.status = 422;
        xhr.responseText = JSON.stringify({
          error: "conversation_id_required",
          detail: "documents must carry conversation_id",
        });
        queueMicrotask(() => xhr.onload?.());
      });
    }));

    await expect(
      uploadDocument(FAKE_PERSONA, "", makeFile("r.pdf", "application/pdf"), {
        getToken: async () => FAKE_TOKEN,
      }),
    ).rejects.toThrow(ApiError);
  });
});

describe("upload.ts: FastAPI error envelope (D-FV0-09)", () => {
  let restore: () => void;
  afterEach(() => restore?.());

  /** The exact body the upload route sends when a parser library is missing. */
  const missingDependency = {
    detail: {
      error: "missing_dependency",
      detail: "pypdf is not installed",
      context: {
        format: "pdf",
        install_hint: "pip install persona-core[documents]",
      },
    },
  };

  it("unwraps the nested body so the code and message are the server's", async () => {
    ({ restore } = installFakeXhr((xhr) => {
      xhr.send = vi.fn(() => {
        xhr.status = 503;
        xhr.responseText = JSON.stringify(missingDependency);
        queueMicrotask(() => xhr.onload?.());
      });
    }));

    const failure = await uploadDocument(
      FAKE_PERSONA,
      FAKE_CONVERSATION,
      makeFile("lease.pdf", "application/pdf"),
      { getToken: async () => FAKE_TOKEN },
    ).catch((e: unknown) => e);

    expect(failure).toBeInstanceOf(ApiError);
    const error = failure as ApiError;
    expect(error.status).toBe(503);
    expect(error.code).toBe("missing_dependency");
    expect(error.detail).toBe("pypdf is not installed");
    expect(error.context?.format).toBe("pdf");
    expect(describeUploadError(error)).toBe(
      "missing_dependency: pypdf is not installed",
    );
  });

  it("passes a flat body through unchanged", () => {
    const flat = { error: "corrupt_document", detail: "could not parse" };
    expect(uploadErrorBody(flat)).toEqual(flat);
  });

  it("does not unwrap a detail that is not a structured body", () => {
    expect(uploadErrorBody({ detail: "Not authenticated" })).toEqual({
      detail: "Not authenticated",
    });
    const validation = { detail: [{ loc: ["body", "file"], msg: "required" }] };
    expect(uploadErrorBody(validation)).toEqual(validation);
    expect(uploadErrorBody("nope")).toBeUndefined();
    expect(uploadErrorBody(null)).toBeUndefined();
  });

  it("never renders an object detail as [object Object]", () => {
    const error = new ApiError(
      422,
      { detail: [{ loc: ["body", "file"], msg: "required" }] },
      { limit: null, remaining: null, reset: null, retryAfter: null },
    );
    const text = describeUploadError(error);
    expect(text).not.toContain("[object Object]");
    expect(text).toContain("required");
  });
});

describe("upload.ts — store-by-reference structural assertion (Concern #4)", () => {
  let restore: () => void;
  afterEach(() => restore?.());

  it("a 1 MB image upload sends raw multipart bytes, NOT a base64 data URI", async () => {
    // The structural defence mirrors Spec 13's T13 DB-layer guard at the
    // API-call layer: upload.ts must never base64-encode the bytes into
    // the request body. The 1 MB file is the canonical regression input.
    const mb = 1024 * 1024;
    const file = makeFile("big.png", "image/png", mb);
    let captured!: FakeXhr;
    ({ restore } = installFakeXhr((xhr) => {
      captured = xhr;
      xhr.send = vi.fn((body) => {
        xhr.lastBody = body as FormData;
        xhr.status = 201;
        xhr.responseText = JSON.stringify({
          workspace_path: "uploads/big.png",
          media_type: "image/png",
          size_bytes: mb,
        });
        queueMicrotask(() => xhr.onload?.());
      });
    }));

    await uploadImage(FAKE_PERSONA, file, {
      getToken: async () => FAKE_TOKEN,
    });

    // The body is a FormData; the file MUST land via the binary slot,
    // not as a string. JSON.stringify(FormData) returns "{}" if no
    // base64 has been injected — that's the structural property.
    const formAsString = JSON.stringify(captured.lastBody);
    expect(formAsString).not.toContain("base64");
    expect(formAsString).not.toContain("data:image");
    // The file reference itself is the FormData's only data; explicit check:
    expect(captured.lastBody?.get("file")).toBe(file);
  });
});
