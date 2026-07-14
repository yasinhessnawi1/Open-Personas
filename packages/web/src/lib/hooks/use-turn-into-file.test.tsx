/**
 * R9-050 — `useTurnIntoFile`: the loading state must PERSIST until the
 * `file_extract` job actually lands (a bounded poll of the Files panel's own
 * resource, keyed off a pre-fire baseline snapshot — see the hook's module
 * docstring for the full completion-signal decision), not vanish the instant
 * the 202 comes back.
 */

import { act, renderHook } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/auth", () => ({
  useAuth: () => ({ getToken: () => Promise.resolve("test-token") }),
}));

const turnMessageIntoFileMock = vi.fn();
vi.mock("@/lib/turn-into-file", () => ({
  turnMessageIntoFile: (...args: unknown[]) => turnMessageIntoFileMock(...args),
}));

const artifactsGetMock = vi.fn();
vi.mock("@/lib/api/client", () => ({
  createApiClient: () => ({
    GET: (...args: unknown[]) => artifactsGetMock(...args),
  }),
}));

const toastFns = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
  info: vi.fn(),
  warning: vi.fn(),
  loading: vi.fn(() => "toast-id-1"),
}));
vi.mock("@/components/patterns/toast", () => ({ toast: toastFns }));

import { NotificationProvider } from "@/components/providers/notification-provider";
import { useTurnIntoFile } from "./use-turn-into-file";

const messages = {
  chat: {
    actions: {
      turnIntoFile: {
        toast: "Creating your file…",
        errorToast: "Couldn't start the file — try again",
        successToast: "File created — added to Files",
        timeoutToast: "Still working on your file — check Files in a moment",
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

function emptyArtifacts() {
  return Promise.resolve({ data: { items: [] } });
}

function artifactsWith(ref: string) {
  return Promise.resolve({
    data: { items: [{ ref, size_bytes: 1, media_type: "application/pdf" }] },
  });
}

describe("useTurnIntoFile — R9-050 persistent loading", () => {
  const POLL_INTERVAL = 10;
  const POLL_MAX = 30;

  beforeEach(() => {
    vi.useFakeTimers();
    turnMessageIntoFileMock.mockReset();
    artifactsGetMock.mockReset();
    toastFns.loading.mockClear();
    toastFns.success.mockClear();
    toastFns.error.mockClear();
    toastFns.warning.mockClear();
    toastFns.info.mockClear();
  });

  afterEach(() => {
    vi.runOnlyPendingTimers();
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  function renderTurnIntoFile() {
    return renderHook(
      () =>
        useTurnIntoFile({
          conversationId: "conv_1",
          personaId: "persona_1",
          pollIntervalMs: POLL_INTERVAL,
          pollMaxMs: POLL_MAX,
        }),
      { wrapper },
    );
  }

  it("shows a persistent loading toast the moment it fires — no toast is shown then silently dropped", async () => {
    turnMessageIntoFileMock.mockReturnValue(new Promise(() => {})); // never resolves within this test
    artifactsGetMock.mockImplementation(emptyArtifacts);
    const { result } = renderTurnIntoFile();

    await act(async () => {
      result.current("msg_1", "auto");
      await vi.advanceTimersByTimeAsync(0);
    });

    expect(toastFns.loading).toHaveBeenCalledWith(
      "Creating your file…",
      undefined,
    );
    // No premature resolution — the loading toast is still the only thing shown.
    expect(toastFns.success).not.toHaveBeenCalled();
    expect(toastFns.error).not.toHaveBeenCalled();
    expect(toastFns.warning).not.toHaveBeenCalled();
  });

  it("resolves the SAME toast (by id) to success once a new artifact lands in the poll", async () => {
    turnMessageIntoFileMock.mockResolvedValue({
      job_id: "job_1",
      status: "queued",
    });
    artifactsGetMock
      .mockImplementationOnce(emptyArtifacts) // baseline snapshot (pre-fire)
      .mockImplementationOnce(emptyArtifacts) // poll tick 1 — not yet
      .mockImplementationOnce(() => artifactsWith("board-plan-abc.pdf")); // poll tick 2 — landed
    const { result } = renderTurnIntoFile();

    await act(async () => {
      result.current("msg_1", "auto");
      await vi.advanceTimersByTimeAsync(0); // flush baseline fetch + POST resolution
    });

    expect(toastFns.loading).toHaveBeenCalledTimes(1);
    const toastId = toastFns.loading.mock.results[0]?.value;

    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL); // tick 1 — still nothing
    });
    expect(toastFns.success).not.toHaveBeenCalled();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL); // tick 2 — the file landed
    });
    expect(toastFns.success).toHaveBeenCalledWith(
      "File created — added to Files",
      { id: toastId },
    );
    expect(toastFns.error).not.toHaveBeenCalled();
    expect(toastFns.warning).not.toHaveBeenCalled();
  });

  it("falls back to an honest 'still working' message on timeout — never a false error", async () => {
    turnMessageIntoFileMock.mockResolvedValue({
      job_id: "job_1",
      status: "queued",
    });
    artifactsGetMock.mockImplementation(emptyArtifacts); // never a new ref

    const { result } = renderTurnIntoFile();
    await act(async () => {
      result.current("msg_1", "auto");
      await vi.advanceTimersByTimeAsync(0);
    });
    const toastId = toastFns.loading.mock.results[0]?.value;

    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MAX + POLL_INTERVAL);
    });

    expect(toastFns.warning).toHaveBeenCalledWith(
      "Still working on your file — check Files in a moment",
      { id: toastId },
    );
    expect(toastFns.error).not.toHaveBeenCalled();
    expect(toastFns.success).not.toHaveBeenCalled();
  });

  it("swaps straight to an honest error when the enqueue itself fails — never starts a poll", async () => {
    turnMessageIntoFileMock.mockRejectedValue(new Error("wrong_message_role"));
    artifactsGetMock.mockImplementation(emptyArtifacts);

    const { result } = renderTurnIntoFile();
    await act(async () => {
      result.current("msg_1", "auto");
      await vi.advanceTimersByTimeAsync(0);
    });
    const toastId = toastFns.loading.mock.results[0]?.value;

    expect(toastFns.error).toHaveBeenCalledWith(
      "Couldn't start the file — try again",
      { id: toastId },
    );

    const callsAtFailure = artifactsGetMock.mock.calls.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MAX * 3); // far past the ceiling
    });
    // No poll was ever started off the back of a failed enqueue.
    expect(artifactsGetMock.mock.calls.length).toBe(callsAtFailure);
    expect(toastFns.success).not.toHaveBeenCalled();
    expect(toastFns.warning).not.toHaveBeenCalled();
  });

  it("a double-click on the SAME message+format while one is in flight is a no-op (idempotent, matches the backend's own key)", async () => {
    turnMessageIntoFileMock.mockReturnValue(new Promise(() => {})); // stays in flight
    artifactsGetMock.mockImplementation(emptyArtifacts);

    const { result } = renderTurnIntoFile();
    await act(async () => {
      result.current("msg_1", "auto");
      result.current("msg_1", "auto");
      await vi.advanceTimersByTimeAsync(0);
    });

    expect(turnMessageIntoFileMock).toHaveBeenCalledTimes(1);
    expect(toastFns.loading).toHaveBeenCalledTimes(1);
  });

  it("a different message (or format) while one is in flight is NOT blocked", async () => {
    turnMessageIntoFileMock.mockReturnValue(new Promise(() => {}));
    artifactsGetMock.mockImplementation(emptyArtifacts);

    const { result } = renderTurnIntoFile();
    await act(async () => {
      result.current("msg_1", "auto");
      result.current("msg_2", "auto");
      result.current("msg_1", "pdf");
      await vi.advanceTimersByTimeAsync(0);
    });

    expect(turnMessageIntoFileMock).toHaveBeenCalledTimes(3);
    expect(toastFns.loading).toHaveBeenCalledTimes(3);
  });
});
