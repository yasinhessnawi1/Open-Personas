/**
 * Issue #16 - the two controls the task and routine hand-off dialogs gained.
 *
 * Chat has had both for a long time; handing a persona a task had neither. These tests
 * pin the part that matters beyond the pixels: the attached file's workspace ref reaches
 * the create call, which is what puts it on the task contract the persona reads. The
 * dictation leg goes through the SAME stt seam the chat composer uses, so there is one
 * dictation control in the product, not two.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NewTaskDialog } from "@/components/activity/new-task-dialog";
import { CreateReminderDialog } from "@/components/schedule/create-reminder-dialog";
import messages from "@/i18n/messages/en.json";

const push = vi.hoisted(() => vi.fn());
const createSchedule = vi.hoisted(() =>
  vi.fn().mockResolvedValue({ task_id: "t_new", schedule_id: "s_new" }),
);
const previewCreate = vi.hoisted(() => vi.fn());
const uploadTaskAttachment = vi.hoisted(() => vi.fn());
const transcribeAudio = vi.hoisted(() => vi.fn());

vi.mock("@/auth", () => ({
  useAuth: () => ({ getToken: () => Promise.resolve("test-token") }),
}));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));
vi.mock("@/lib/api/schedule-client", () => ({ createSchedule, previewCreate }));
vi.mock("@/lib/upload", () => ({ uploadTaskAttachment }));
vi.mock("@/lib/voice/stt", () => ({
  transcribeAudio: (...args: unknown[]) => transcribeAudio(...args),
}));

const PERSONAS = [
  { id: "kai", name: "Kai" },
  { id: "iris", name: "Iris" },
];

const REF = {
  ref: "uploads/9f2c0a.md",
  filename: "brief.md",
  media_type: "text/markdown",
};

/** A scripted MediaRecorder double - jsdom has no real implementation. */
class FakeMediaRecorder {
  static isTypeSupported = vi.fn(() => true);
  state: "inactive" | "recording" = "inactive";
  mimeType = "audio/webm";
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

const fakeStream = {
  getTracks: () => [{ stop: vi.fn() }],
} as unknown as MediaStream;

function aFile(name = "brief.md") {
  return new File(["# hello"], name, { type: "text/markdown" });
}

function attachButton(): HTMLInputElement {
  return screen.getByLabelText("Attach a file") as HTMLInputElement;
}

beforeEach(() => {
  vi.clearAllMocks();
  uploadTaskAttachment.mockResolvedValue(REF);
  createSchedule.mockResolvedValue({ task_id: "t_new", schedule_id: "s_new" });
  previewCreate.mockResolvedValue({
    human_terms: "every day at 09:00",
    timezone: "Europe/Oslo",
    next_fire: "2026-07-06T07:00:00Z",
    quiet_hours_offer: null,
  });
  vi.stubGlobal("MediaRecorder", FakeMediaRecorder);
  Object.defineProperty(global.navigator, "mediaDevices", {
    value: { getUserMedia: vi.fn().mockResolvedValue(fakeStream) },
    configurable: true,
    writable: true,
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

// --- the task hand-off dialog -------------------------------------------------------

describe("the task hand-off dialog", () => {
  function open() {
    render(
      <NextIntlClientProvider locale="en" messages={messages}>
        <NewTaskDialog personas={PERSONAS} action={vi.fn()} />
      </NextIntlClientProvider>,
    );
    fireEvent.click(screen.getByRole("button", { name: /new task/i }));
  }

  async function pickPersona(name = "Kai") {
    fireEvent.click(screen.getByLabelText("Who runs it?"));
    fireEvent.click(
      await screen.findByRole("menuitem", { name: new RegExp(name) }),
    );
  }

  it("offers an attach button and a dictation button on the description", () => {
    open();
    expect(attachButton()).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Dictate with your voice" }),
    ).toBeInTheDocument();
  });

  it("shows a chip for an attached file and carries its ref into the start payload", async () => {
    open();
    await pickPersona();
    fireEvent.change(attachButton(), { target: { files: [aFile()] } });

    await waitFor(() => expect(uploadTaskAttachment).toHaveBeenCalledTimes(1));
    expect(uploadTaskAttachment.mock.calls[0][0]).toBe("kai");
    expect(await screen.findByText("brief.md")).toBeInTheDocument();

    // The Start-now door is a form action, so the landed ref travels as a form field.
    const hidden = document.querySelector<HTMLInputElement>(
      'input[name="attachments"]',
    );
    await waitFor(() =>
      expect(JSON.parse(hidden?.value ?? "[]")).toEqual([REF]),
    );
  });

  it("carries the ref into the schedule-for-later payload too", async () => {
    open();
    fireEvent.change(screen.getByLabelText("What should get done?"), {
      target: { value: "Summarise the papers" },
    });
    await pickPersona();
    fireEvent.change(attachButton(), { target: { files: [aFile()] } });
    await screen.findByText("brief.md");

    fireEvent.change(screen.getByLabelText("When (optional)"), {
      target: { value: "2027-01-05T09:30" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: /schedule for later/i }),
    );

    await waitFor(() => expect(createSchedule).toHaveBeenCalledTimes(1));
    expect(createSchedule.mock.calls[0][1].attachments).toEqual([REF]);
  });

  it("dictates into the description through the same stt seam the composer uses", async () => {
    transcribeAudio.mockResolvedValue("summarise the quarter");
    open();
    fireEvent.click(
      screen.getByRole("button", { name: "Dictate with your voice" }),
    );
    const stop = await screen.findByRole("button", { name: "Stop recording" });
    fireEvent.click(stop);

    await waitFor(() => expect(transcribeAudio).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(
        (screen.getByLabelText("What should get done?") as HTMLTextAreaElement)
          .value,
      ).toBe("summarise the quarter"),
    );
  });

  it("stops offering attachments at the API's cap", async () => {
    open();
    await pickPersona();
    const many = Array.from({ length: 12 }, (_, i) => aFile(`f${i}.md`));
    fireEvent.change(attachButton(), { target: { files: many } });

    // Ten is what the create schemas take; the eleventh and twelfth never leave the browser.
    await waitFor(() => expect(uploadTaskAttachment).toHaveBeenCalledTimes(10));
    await waitFor(() => expect(attachButton()).toBeDisabled());
  });

  it("holds the hand-off while a file is still going up", async () => {
    let settle: (value: typeof REF) => void = () => {};
    uploadTaskAttachment.mockReturnValue(
      new Promise<typeof REF>((resolve) => {
        settle = resolve;
      }),
    );
    open();
    fireEvent.change(screen.getByLabelText("What should get done?"), {
      target: { value: "Summarise the papers" },
    });
    await pickPersona();
    fireEvent.change(attachButton(), { target: { files: [aFile()] } });

    await waitFor(() =>
      expect(screen.getByRole("button", { name: /start now/i })).toBeDisabled(),
    );
    settle(REF);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /start now/i })).toBeEnabled(),
    );
  });
});

// --- the routine dialog -------------------------------------------------------------

describe("the new routine dialog", () => {
  function open() {
    render(
      <NextIntlClientProvider locale="en" messages={messages}>
        <CreateReminderDialog
          personas={[{ id: "p1", name: "Astrid" }]}
          defaultTimezone="Europe/Oslo"
          onClose={vi.fn()}
          onCreated={vi.fn().mockResolvedValue(undefined)}
        />
      </NextIntlClientProvider>,
    );
  }

  async function pickPersona() {
    fireEvent.click(screen.getByLabelText(/who should run it/i));
    fireEvent.click(await screen.findByRole("menuitem", { name: /Astrid/ }));
  }

  it("offers an attach button and a dictation button", () => {
    open();
    expect(attachButton()).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Dictate with your voice" }),
    ).toBeInTheDocument();
  });

  it("carries the attached ref into the routine it creates", async () => {
    open();
    fireEvent.change(screen.getByLabelText(/what should i do for you/i), {
      target: { value: "stretch for five minutes" },
    });
    await pickPersona();
    fireEvent.change(
      document.querySelector("#recur-time") as HTMLInputElement,
      { target: { value: "08:30" } },
    );
    fireEvent.change(attachButton(), { target: { files: [aFile()] } });
    await screen.findByText("brief.md");

    fireEvent.click(screen.getByRole("button", { name: /^preview$/i }));
    const confirm = await screen.findByRole("button", { name: /^confirm$/i });
    fireEvent.click(confirm);

    await waitFor(() => expect(createSchedule).toHaveBeenCalledTimes(1));
    expect(createSchedule.mock.calls[0][1].attachments).toEqual([REF]);
  });

  it("dictates into the subject through the same stt seam", async () => {
    transcribeAudio.mockResolvedValue("water the plants");
    open();
    fireEvent.click(
      screen.getByRole("button", { name: "Dictate with your voice" }),
    );
    fireEvent.click(
      await screen.findByRole("button", { name: "Stop recording" }),
    );

    await waitFor(() => expect(transcribeAudio).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(
        (screen.getByLabelText(/what should i do for you/i) as HTMLInputElement)
          .value,
      ).toBe("water the plants"),
    );
  });
});
