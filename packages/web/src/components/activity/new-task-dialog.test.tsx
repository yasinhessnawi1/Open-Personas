/**
 * Spec R11 (B2) — <NewTaskDialog> tests: the kit's "Hand a task to a persona"
 * dialog. Two real doors: Start now (the /runs dispatch action) and Schedule
 * for later (the A10 one-door with intent:"task" — the goal rides verbatim).
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { beforeEach, describe, expect, it, vi } from "vitest";

import messages from "@/i18n/messages/en.json";

import { NewTaskCta, NewTaskDialog } from "./new-task-dialog";

const push = vi.hoisted(() => vi.fn());
const createSchedule = vi.hoisted(() =>
  vi.fn().mockResolvedValue({ task_id: "t_new", schedule_id: "s_new" }),
);

vi.mock("@/auth", () => ({
  useAuth: () => ({ getToken: () => Promise.resolve("test-token") }),
}));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));
vi.mock("@/lib/api/schedule-client", () => ({ createSchedule }));

const PERSONAS = [
  { id: "kai", name: "Kai" },
  { id: "iris", name: "Iris" },
];

function renderDialog(action = vi.fn()) {
  render(
    <NextIntlClientProvider locale="en" messages={messages}>
      <NewTaskDialog personas={PERSONAS} action={action} />
    </NextIntlClientProvider>,
  );
  return action;
}

function fillGoalAndPersona(personaId = "kai") {
  fireEvent.change(screen.getByLabelText("What should get done?"), {
    target: { value: "Summarise the papers" },
  });
  fireEvent.change(screen.getByLabelText("Who runs it?"), {
    target: { value: personaId },
  });
}

beforeEach(() => {
  push.mockClear();
  createSchedule.mockClear();
});

describe("NewTaskDialog (R11-B2)", () => {
  it("opens from the trigger and gates Start now until goal + persona are set", () => {
    renderDialog();
    fireEvent.click(screen.getByRole("button", { name: /new task/i }));
    expect(
      screen.getByRole("heading", { name: "Hand a task to a persona" }),
    ).toBeInTheDocument();

    const start = screen.getByRole("button", { name: /start now/i });
    expect(start).toBeDisabled();

    fireEvent.change(screen.getByLabelText("What should get done?"), {
      target: { value: "Summarise the papers" },
    });
    expect(start).toBeDisabled(); // still no executor

    fireEvent.change(screen.getByLabelText("Who runs it?"), {
      target: { value: "kai" },
    });
    expect(start).toBeEnabled();
  });

  it("previews the hand-off in plain words once ready (kit preview line)", () => {
    renderDialog();
    fireEvent.click(screen.getByRole("button", { name: /new task/i }));
    fillGoalAndPersona("iris");
    const preview = screen.getByText(
      (_, el) =>
        el?.tagName === "P" &&
        /will work toward this in the background/i.test(el.textContent ?? ""),
    );
    expect(preview.querySelector("b")?.textContent).toBe("Iris");
  });

  it("schedules for later through the A10 door with intent:'task' (kit footer leg)", async () => {
    renderDialog();
    fireEvent.click(screen.getByRole("button", { name: /new task/i }));
    fillGoalAndPersona("kai");

    // a future instant flips the action to Schedule for later
    fireEvent.change(screen.getByLabelText("When (optional)"), {
      target: { value: "2027-01-05T09:30" },
    });
    const scheduleBtn = screen.getByRole("button", {
      name: /schedule for later/i,
    });
    expect(scheduleBtn).toBeEnabled();
    expect(screen.queryByRole("button", { name: /start now/i })).toBeNull();

    fireEvent.click(scheduleBtn);
    await waitFor(() => expect(createSchedule).toHaveBeenCalledTimes(1));
    const [, body] = createSchedule.mock.calls[0];
    expect(body.intent).toBe("task");
    expect(body.subject).toBe("Summarise the papers");
    expect(body.persona_id).toBe("kai");
    expect(body.pattern).toBeNull();
    expect(body.one_time_at).toBe(new Date("2027-01-05T09:30").toISOString());
    await waitFor(() =>
      expect(push).toHaveBeenCalledWith("/activity/tasks/t_new"),
    );
  });

  it("refuses a past instant honestly — no silent no-op", () => {
    renderDialog();
    fireEvent.click(screen.getByRole("button", { name: /new task/i }));
    fillGoalAndPersona();
    fireEvent.change(screen.getByLabelText("When (optional)"), {
      target: { value: "2020-01-05T09:30" },
    });
    expect(screen.getByText("Pick a time in the future.")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /schedule for later/i }),
    ).toBeDisabled();
    expect(createSchedule).not.toHaveBeenCalled();
  });

  it("renders nothing without personas (no dead-end dialog)", () => {
    const { container } = render(
      <NextIntlClientProvider locale="en" messages={messages}>
        <NewTaskDialog personas={[]} action={vi.fn()} />
      </NextIntlClientProvider>,
    );
    expect(container).toBeEmptyDOMElement();
  });
});

describe("NewTaskCta (R11-B2)", () => {
  it("renders the hand-off invitation with the dialog trigger", () => {
    render(
      <NextIntlClientProvider locale="en" messages={messages}>
        <NewTaskCta personas={PERSONAS} action={vi.fn()} />
      </NextIntlClientProvider>,
    );
    expect(screen.getByText("Hand off something new")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /new task/i }),
    ).toBeInTheDocument();
  });
});
