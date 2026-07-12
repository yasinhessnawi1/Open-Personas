/**
 * R9-025a — MicDictation tests: record (mocked MediaRecorder) -> transcribe
 * (mocked stt proxy) -> insert into the host's value; permission-denied is a
 * visible error state; an unsupported/unconfigured environment hides the
 * button entirely (fail-soft).
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { useRef, useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const transcribeAudioMock = vi.fn();
vi.mock("@/lib/voice/stt", () => ({
  transcribeAudio: (...args: unknown[]) => transcribeAudioMock(...args),
}));
vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ getToken: () => Promise.resolve("test-token") }),
}));
vi.mock("@/auth", () => ({
  useAuth: () => ({ getToken: () => Promise.resolve("test-token") }),
}));

import { MicDictation } from "./mic-dictation";

const messages = {
  mic: {
    start: "Dictate with your voice",
    stop: "Stop recording",
    transcribing: "Transcribing…",
    permissionDenied:
      "Microphone access denied — allow it in your browser settings to dictate",
  },
};

/** A scripted MediaRecorder double — jsdom has no real implementation. */
class FakeMediaRecorder {
  static isTypeSupported = vi.fn(() => true);
  state: "inactive" | "recording" = "inactive";
  mimeType: string;
  ondataavailable: ((e: { data: Blob }) => void) | null = null;
  onstop: (() => void) | null = null;

  constructor(
    public stream: MediaStream,
    options?: { mimeType?: string },
  ) {
    this.mimeType = options?.mimeType ?? "audio/webm";
  }

  start() {
    this.state = "recording";
  }

  stop() {
    this.state = "inactive";
    this.ondataavailable?.({
      data: new Blob([new Uint8Array([1, 2, 3])], { type: this.mimeType }),
    });
    this.onstop?.();
  }
}

const fakeTrack = { stop: vi.fn() };
const fakeStream = {
  getTracks: () => [fakeTrack],
} as unknown as MediaStream;

function Harness({ initial = "" }: { initial?: string }) {
  const [value, setValue] = useState(initial);
  const ref = useRef<HTMLTextAreaElement>(null);
  return (
    <div>
      <textarea
        ref={ref}
        value={value}
        onChange={(e) => setValue(e.target.value)}
        data-testid="host-textarea"
      />
      <MicDictation value={value} onChange={setValue} textareaRef={ref} />
    </div>
  );
}

function renderHarness(initial?: string) {
  return render(
    <NextIntlClientProvider locale="en" messages={messages}>
      <Harness initial={initial} />
    </NextIntlClientProvider>,
  );
}

describe("MicDictation", () => {
  let getUserMediaMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    transcribeAudioMock.mockReset();
    fakeTrack.stop.mockReset();
    getUserMediaMock = vi.fn().mockResolvedValue(fakeStream);
    vi.stubGlobal("MediaRecorder", FakeMediaRecorder);
    Object.defineProperty(global.navigator, "mediaDevices", {
      value: { getUserMedia: getUserMediaMock },
      configurable: true,
      writable: true,
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("records, transcribes, and inserts the transcript into the host value", async () => {
    transcribeAudioMock.mockResolvedValue("buy milk tomorrow");
    renderHarness();

    fireEvent.click(
      screen.getByRole("button", { name: "Dictate with your voice" }),
    );
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Stop recording" }),
      ).toBeTruthy(),
    );
    expect(getUserMediaMock).toHaveBeenCalledWith({ audio: true });

    fireEvent.click(screen.getByRole("button", { name: "Stop recording" }));

    await waitFor(() =>
      expect(screen.getByTestId("host-textarea")).toHaveValue(
        "buy milk tomorrow",
      ),
    );
    // The mic stream is released once recording stops.
    expect(fakeTrack.stop).toHaveBeenCalled();
    // Back to the idle affordance once the insert lands.
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Dictate with your voice" }),
      ).toBeTruthy(),
    );
  });

  it("appends after existing text with a separating space (caret at end)", async () => {
    transcribeAudioMock.mockResolvedValue("and eggs");
    renderHarness("buy milk");

    fireEvent.click(
      screen.getByRole("button", { name: "Dictate with your voice" }),
    );
    await waitFor(() => screen.getByRole("button", { name: "Stop recording" }));
    fireEvent.click(screen.getByRole("button", { name: "Stop recording" }));

    await waitFor(() =>
      expect(screen.getByTestId("host-textarea")).toHaveValue(
        "buy milk and eggs",
      ),
    );
  });

  it("shows a visible permission-denied state (NOT hidden) on getUserMedia rejection", async () => {
    getUserMediaMock.mockRejectedValue(
      new DOMException("denied", "NotAllowedError"),
    );
    renderHarness();

    fireEvent.click(
      screen.getByRole("button", { name: "Dictate with your voice" }),
    );
    await waitFor(() =>
      expect(
        screen.getByRole("button", {
          name: "Microphone access denied — allow it in your browser settings to dictate",
        }),
      ).toBeTruthy(),
    );
  });

  it("does not call transcribeAudio while recording is in progress", async () => {
    renderHarness();
    fireEvent.click(
      screen.getByRole("button", { name: "Dictate with your voice" }),
    );
    await waitFor(() => screen.getByRole("button", { name: "Stop recording" }));
    expect(transcribeAudioMock).not.toHaveBeenCalled();
  });

  it("is hidden entirely when MediaRecorder is unsupported (fail-soft)", async () => {
    vi.unstubAllGlobals();
    // @ts-expect-error — simulate an environment with no MediaRecorder at all.
    global.MediaRecorder = undefined;
    renderHarness();
    fireEvent.click(
      screen.getByRole("button", { name: "Dictate with your voice" }),
    );
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: "Dictate with your voice" }),
      ).toBeNull(),
    );
  });

  it("respects the disabled prop", () => {
    render(
      <NextIntlClientProvider locale="en" messages={messages}>
        <MicDictation
          value=""
          onChange={() => {}}
          textareaRef={{ current: null }}
          disabled
        />
      </NextIntlClientProvider>,
    );
    const button = screen.getByRole("button", {
      name: "Dictate with your voice",
    }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
  });
});
