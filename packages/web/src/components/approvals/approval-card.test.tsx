/**
 * Spec A6 (W4) — <ApprovalCard> tests: the safety properties, not polish.
 *
 * - **See-then-grant** — the Approve button is unreachable until the exact proposal is seen.
 * - **Faithful render** — the recorded arguments show verbatim (as text).
 * - **Calm reflection (A6-D-3)** — a race loser reflects "Already handled", never an error.
 */
import { fireEvent, render } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it, vi } from "vitest";

import messages from "@/i18n/messages/en.json";
import type { ApprovalOut } from "@/lib/api/approvals-client";

import { ApprovalCard, type CardResolution } from "./approval-card";

const APPROVAL: ApprovalOut = {
  proposal_id: "p1",
  task_id: "t1",
  persona_id: "kai",
  tool_name: "send_email",
  arguments: {
    to: "bjorn@example.com",
    subject: "the deposit",
    body: "Hi Bjørn — please confirm the return.",
  },
  description: "Reply to the landlord about the deposit",
  categories: ["communicate_as_user"],
  created_at: new Date().toISOString(),
  expires_at: new Date(Date.now() + 2 * 86_400_000).toISOString(),
};

function renderCard(resolution: CardResolution | null = null) {
  const onDecide = vi.fn();
  const utils = render(
    <NextIntlClientProvider locale="en" messages={messages}>
      <ApprovalCard
        approval={APPROVAL}
        personaName="Kai"
        resolution={resolution}
        busy={false}
        onDecide={onDecide}
      />
    </NextIntlClientProvider>,
  );
  return { ...utils, onDecide };
}

describe("ApprovalCard", () => {
  it("gates the grant behind seeing the proposal (see-then-grant)", () => {
    const { getByText, queryByText } = renderCard();
    expect(getByText("Reply to the landlord about the deposit")).toBeTruthy();
    expect(queryByText("Approve")).toBeNull(); // not reachable while collapsed

    fireEvent.click(getByText("Review the request"));

    expect(getByText("Approve")).toBeTruthy(); // seen → grantable
    expect(getByText("bjorn@example.com")).toBeTruthy(); // faithful, verbatim
    expect(getByText("Hi Bjørn — please confirm the return.")).toBeTruthy();
  });

  it("approve fires an approve decision", () => {
    const { getByText, onDecide } = renderCard();
    fireEvent.click(getByText("Review the request"));
    fireEvent.click(getByText("Approve"));
    expect(onDecide).toHaveBeenCalledWith({ decision: "approve" });
  });

  it("reflects 'Already handled' from the durable record (never an error)", () => {
    const { getByText, queryByText } = renderCard({
      outcome: null,
      status: "consumed",
      note: "not_pending",
    });
    expect(getByText("Already handled.")).toBeTruthy();
    expect(queryByText("Review the request")).toBeNull(); // resolved → the card rests
  });

  it("reflects an approval", () => {
    const { getByText } = renderCard({
      outcome: "approve",
      status: "consumed",
      note: "",
    });
    expect(getByText("Approved.")).toBeTruthy();
  });
});
