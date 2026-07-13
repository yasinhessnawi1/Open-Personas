/**
 * Spec R11 (B2) — <NewTaskDialog> tests: the kit's "Hand a task to a persona"
 * dialog, honest-subset (goal + executor over the REAL /runs dispatch door).
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it, vi } from "vitest";

import messages from "@/i18n/messages/en.json";

import { NewTaskCta, NewTaskDialog } from "./new-task-dialog";

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
    fireEvent.change(screen.getByLabelText("What should get done?"), {
      target: { value: "Summarise the papers" },
    });
    fireEvent.change(screen.getByLabelText("Who runs it?"), {
      target: { value: "iris" },
    });
    const preview = screen.getByText(
      (_, el) =>
        el?.tagName === "P" &&
        /will work toward this in the background/i.test(el.textContent ?? ""),
    );
    expect(preview.querySelector("b")?.textContent).toBe("Iris");
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
