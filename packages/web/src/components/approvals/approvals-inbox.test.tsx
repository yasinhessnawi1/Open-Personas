/**
 * The inbox's reopened half (part1 F10).
 *
 * A decision used to leave nothing behind that a fresh page could read: the pending list drops
 * a proposal the moment it is answered, so "Approved with your edits" lived exactly as long as
 * the tab that showed it. These tests load the component cold, with only what the server would
 * hand it, and ask what it says.
 */
import { render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { beforeEach, describe, expect, it, vi } from "vitest";

import messages from "@/i18n/messages/en.json";
import type { ApprovalOut } from "@/lib/api/approvals-client";

import { ApprovalsInbox } from "./approvals-inbox";

const api = vi.hoisted(() => ({
  fetchApprovals: vi.fn(),
  fetchHandledApprovals: vi.fn(),
  decideApproval: vi.fn(),
  getApproval: vi.fn(),
}));

vi.mock("@/auth", () => ({
  useAuth: () => ({ getToken: () => Promise.resolve("test-token") }),
}));
vi.mock("@/lib/api/approvals-client", () => api);
vi.mock("@/components/patterns/toast", () => ({
  useToast: () => ({ info: vi.fn(), success: vi.fn(), error: vi.fn() }),
}));
vi.mock("@/lib/task-signal", () => ({ useTaskSignal: () => {} }));

function approval(over: Partial<ApprovalOut> = {}): ApprovalOut {
  return {
    proposal_id: "p1",
    task_id: "t1",
    persona_id: "kai",
    tool_name: "send_email",
    arguments: { to: "bjorn@example.com" },
    description: "Reply to the landlord about the deposit",
    categories: ["communicate_as_user"],
    created_at: new Date().toISOString(),
    expires_at: new Date(Date.now() + 2 * 86_400_000).toISOString(),
    status: "pending",
    edited: false,
    ...over,
  };
}

function renderInbox() {
  return render(
    <NextIntlClientProvider locale="en" messages={messages}>
      <ApprovalsInbox personaNames={{ kai: "Kai" }} />
    </NextIntlClientProvider>,
  );
}

describe("ApprovalsInbox", () => {
  beforeEach(() => {
    api.fetchApprovals.mockReset();
    api.fetchHandledApprovals.mockReset();
  });

  it("shows what the record says about an edited approval on a fresh load", async () => {
    api.fetchApprovals.mockResolvedValue([]);
    api.fetchHandledApprovals.mockResolvedValue([
      approval({ status: "consumed", edited: true }),
    ]);

    renderInbox();

    await waitFor(() =>
      expect(screen.getByText("Approved with your edits.")).toBeTruthy(),
    );
    expect(screen.getByText("Recently handled")).toBeTruthy();
    // Nothing is waiting, so the empty state still speaks for the pending half.
    expect(screen.getByText("Nothing waiting on you")).toBeTruthy();
  });

  it("does not repeat a proposal that is in both halves", async () => {
    api.fetchApprovals.mockResolvedValue([approval()]);
    api.fetchHandledApprovals.mockResolvedValue([approval()]);

    renderInbox();

    await waitFor(() =>
      expect(
        screen.getAllByText("Reply to the landlord about the deposit"),
      ).toHaveLength(1),
    );
    expect(screen.queryByText("Recently handled")).toBeNull();
  });

  it("says nothing about a history that is empty", async () => {
    api.fetchApprovals.mockResolvedValue([]);
    api.fetchHandledApprovals.mockResolvedValue([]);

    renderInbox();

    await waitFor(() =>
      expect(screen.getByText("Nothing waiting on you")).toBeTruthy(),
    );
    expect(screen.queryByText("Recently handled")).toBeNull();
  });
});
