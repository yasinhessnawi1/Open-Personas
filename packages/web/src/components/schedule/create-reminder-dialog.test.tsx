/**
 * Spec A10 (T5/T6) — the "New reminder" flow: build → preview → confirm → create,
 * quiet-hours accept-the-edge/override, and the per-dialog idempotency key (A10-D-6).
 * The picker is A8's builder REUSED (the dialog renders `recurrence-builder`'s testid —
 * grep-proof of reuse, not re-authoring); the client fns are mocked at the module seam.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ReschedulePreview } from "@/lib/api/schedule-client";
import { applyQuietEdge, CreateReminderDialog } from "./create-reminder-dialog";
import type { CadenceInput } from "./recurrence-builder";

const client = vi.hoisted(() => ({
  previewCreate: vi.fn(),
  createSchedule: vi.fn(),
}));

vi.mock("@/auth", () => ({
  useAuth: () => ({ getToken: () => Promise.resolve("test-token") }),
}));
vi.mock("@/lib/api/schedule-client", () => ({
  previewCreate: client.previewCreate,
  createSchedule: client.createSchedule,
}));

const _PREVIEW: ReschedulePreview = {
  human_terms: "every day at 09:00",
  timezone: "Europe/Oslo",
  next_fire: "2026-07-06T07:00:00Z",
  quiet_hours_offer: null,
};

function makeDialog(onCreated = vi.fn().mockResolvedValue(undefined)) {
  const onClose = vi.fn();
  render(
    <CreateReminderDialog
      personas={[
        { id: "p1", name: "Astrid" },
        { id: "p2", name: "Iris" },
      ]}
      defaultTimezone="Europe/Oslo"
      onClose={onClose}
      onCreated={onCreated}
    />,
  );
  return { onClose, onCreated };
}

function timeInput(): HTMLInputElement {
  // The builder's label wraps text + input + tz span (mixed content), which defeats
  // getByLabelText — select the reused A8 input by its stable id instead.
  return document.querySelector("#recur-time") as HTMLInputElement;
}

function fillRequired() {
  fireEvent.change(screen.getByLabelText(/what should i remind you/i), {
    target: { value: "stretch for five minutes" },
  });
  fireEvent.change(screen.getByLabelText(/who should run it/i), {
    target: { value: "p1" },
  });
  // Touch the reused A8 builder (the time input) so it emits a cadence.
  fireEvent.change(timeInput(), { target: { value: "08:30" } });
}

beforeEach(() => {
  vi.clearAllMocks();
  client.previewCreate.mockResolvedValue(_PREVIEW);
  client.createSchedule.mockResolvedValue({
    task_id: "t",
    schedule_id: "s",
    created: true,
    ..._PREVIEW,
  });
});

describe("CreateReminderDialog", () => {
  it("reuses A8's builder and gates Preview on subject + persona + cadence", () => {
    makeDialog();
    expect(screen.getByTestId("recurrence-builder")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Preview" })).toBeDisabled();
    fillRequired();
    expect(screen.getByRole("button", { name: "Preview" })).toBeEnabled();
  });

  it("previews the engine echo, then confirms through the create door", async () => {
    const onCreated = vi.fn().mockResolvedValue(undefined);
    makeDialog(onCreated);
    fillRequired();

    fireEvent.click(screen.getByRole("button", { name: "Preview" }));
    await waitFor(() =>
      expect(screen.getByTestId("create-preview")).toHaveTextContent(
        "When: every day at 09:00 · Europe/Oslo",
      ),
    );
    expect(client.previewCreate).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(onCreated).toHaveBeenCalledTimes(1));
    const body = client.createSchedule.mock.calls[0][1];
    expect(body.persona_id).toBe("p1");
    expect(body.subject).toBe("stretch for five minutes");
    expect(body.timezone).toBe("Europe/Oslo");
    expect(body.pattern?.kind).toBe("daily");
    expect(body.idempotency_key).toMatch(/[0-9a-f-]{36}/); // minted once per dialog-open
  });

  it("offers the quiet-hours edge; accepting re-times and re-previews (never blocks)", async () => {
    client.previewCreate.mockResolvedValueOnce({
      ..._PREVIEW,
      quiet_hours_offer: "06:00",
    });
    makeDialog();
    fillRequired();

    fireEvent.click(screen.getByRole("button", { name: "Preview" }));
    await waitFor(() =>
      expect(screen.getByTestId("quiet-offer")).toHaveTextContent(
        "inside your quiet hours",
      ),
    );
    // Confirm stays available — override is the user's right (warn, never block).
    expect(screen.getByRole("button", { name: "Confirm" })).toBeEnabled();

    fireEvent.click(screen.getByRole("button", { name: "Move to 06:00" }));
    await waitFor(() => expect(client.previewCreate).toHaveBeenCalledTimes(2));
    const retimed = client.previewCreate.mock.calls[1][1];
    expect(retimed.pattern?.hour).toBe(6);
    expect(retimed.pattern?.minute).toBe(0);
  });

  it("invalidates the preview when the cadence changes (confirm only a fresh echo)", async () => {
    makeDialog();
    fillRequired();
    fireEvent.click(screen.getByRole("button", { name: "Preview" }));
    await waitFor(() =>
      expect(screen.getByTestId("create-preview")).toBeInTheDocument(),
    );

    fireEvent.change(timeInput(), { target: { value: "10:30" } });
    expect(screen.queryByTestId("create-preview")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Preview" })).toBeInTheDocument();
  });
});

describe("applyQuietEdge", () => {
  it("re-times a recurring pattern to the edge", () => {
    const cadence: CadenceInput = {
      pattern: { kind: "daily", hour: 9, minute: 0 },
      one_time_at: null,
    };
    const out = applyQuietEdge(cadence, "06:30");
    expect(out.pattern).toMatchObject({ hour: 6, minute: 30 });
  });

  it("re-times a one-time instant to the edge on the same day", () => {
    const at = new Date();
    at.setHours(9, 0, 0, 0);
    const out = applyQuietEdge(
      { pattern: null, one_time_at: at.toISOString() },
      "06:30",
    );
    const retimed = new Date(out.one_time_at as string);
    expect([retimed.getHours(), retimed.getMinutes()]).toEqual([6, 30]);
  });
});

describe("RecurrenceBuilder one-time kind (A10-D-5)", () => {
  it("emits one_time_at (never a pattern) for Once, at…", async () => {
    const { RecurrenceBuilder } = await import("./recurrence-builder");
    const seen: CadenceInput[] = [];
    render(
      <RecurrenceBuilder
        timezone="Europe/Oslo"
        onChange={(c) => seen.push(c)}
      />,
    );
    fireEvent.change(screen.getByLabelText(/repeats/i), {
      target: { value: "once" },
    });
    fireEvent.change(
      document.querySelector("#recur-once") as HTMLInputElement,
      { target: { value: "2026-08-01T09:00" } },
    );
    const last = seen.at(-1);
    expect(last?.pattern).toBeNull();
    expect(last?.one_time_at).toMatch(/^\d{4}-\d{2}-\d{2}T.*Z$/); // an ISO UTC instant
  });
});
