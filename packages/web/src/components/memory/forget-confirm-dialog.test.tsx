/**
 * Spec K11 (T5, D-K11-1) — `<ForgetConfirmDialog>` component tests.
 *
 * Every candidate starts selected (= will be forgotten); deselecting keeps
 * that piece of evidence alive. Confirms with exactly the kept-selected
 * subset; cancel never calls onConfirm.
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it, vi } from "vitest";
import type { ForgetCandidate } from "@/lib/api";
import { ForgetConfirmDialog } from "./forget-confirm-dialog";

const messages = {
  memory: {
    forgetDialogTitle:
      "Also forget these {count, plural, one {# memory} other {# memories}}?",
    forgetDialogBody:
      "Deleting this would leave behind conversation memory that could recreate it. Choose what to forget too, and deselect anything you'd rather keep.",
    forgetDialogCancel: "Cancel",
    forgetDialogConfirm: "Forget everywhere",
  },
};

const CANDIDATES: ForgetCandidate[] = [
  {
    persona_id: "astrid",
    persona_name: "Astrid Berg",
    chunk_id: "c1",
    kind: "raw",
    text: "USER: Balto is my dog.",
    score: 0.91,
  },
  {
    persona_id: "lena",
    persona_name: "Lena Brevik",
    chunk_id: "c2",
    kind: "raw",
    text: "ASSISTANT: Noted, Balto the dog.",
    score: 0.85,
  },
];

function renderDialog(
  overrides: Partial<React.ComponentProps<typeof ForgetConfirmDialog>> = {},
) {
  const onCancel = vi.fn();
  const onConfirm = vi.fn();
  render(
    <NextIntlClientProvider locale="en" messages={messages}>
      <ForgetConfirmDialog
        open
        candidates={CANDIDATES}
        busy={false}
        onCancel={onCancel}
        onConfirm={onConfirm}
        {...overrides}
      />
    </NextIntlClientProvider>,
  );
  return { onCancel, onConfirm };
}

describe("ForgetConfirmDialog", () => {
  it("renders every candidate persona-labelled, all pre-selected", () => {
    renderDialog();
    expect(
      screen.getByText("Also forget these 2 memories?"),
    ).toBeInTheDocument();
    expect(screen.getByText("Astrid Berg")).toBeInTheDocument();
    expect(screen.getByText("Lena Brevik")).toBeInTheDocument();
    for (const cb of screen.getAllByRole("checkbox")) {
      expect(cb).toBeChecked();
    }
  });

  it("confirms with the full candidate set when nothing is deselected", () => {
    const { onConfirm } = renderDialog();
    fireEvent.click(screen.getByRole("button", { name: "Forget everywhere" }));
    expect(onConfirm).toHaveBeenCalledWith(CANDIDATES);
  });

  it("deselecting one candidate excludes it from the confirmed set", () => {
    const { onConfirm } = renderDialog();
    fireEvent.click(screen.getByRole("checkbox", { name: /Astrid Berg/ }));
    fireEvent.click(screen.getByRole("button", { name: "Forget everywhere" }));
    expect(onConfirm).toHaveBeenCalledWith([CANDIDATES[1]]);
  });

  it("cancel calls onCancel, never onConfirm", () => {
    const { onCancel, onConfirm } = renderDialog();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("does not render when closed", () => {
    renderDialog({ open: false });
    expect(screen.queryByText(/Also forget/)).not.toBeInTheDocument();
  });
});
