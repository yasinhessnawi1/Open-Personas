/**
 * Spec W1 (T7) — the review line's verbs actually act.
 *
 * What is pinned here is the wiring the browser oracle cannot see at this resolution: which
 * verbs a line offers, that each one calls the matching durable command with the task behind
 * that line, that a reply carries the user's own words, that a no-op speaks the server's own
 * sentence instead of failing silently, and that acting re-reads the digest rather than
 * patching the row (refetch-not-trust, A6-R-4; the badge is that list's length, D-W1-5).
 *
 * Approvals are the deliberate non-action: their verbs act on a proposal whose arguments live
 * in the inbox, and see-then-grant says you look before you grant. That line keeps its link.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { beforeEach, describe, expect, it, vi } from "vitest";

import messages from "@/i18n/messages/en.json";
import type { DigestItem } from "@/lib/api/review-client";

import {
  AttentionActions,
  actionableVerbs,
  isActionable,
} from "./attention-actions";

const tasks = vi.hoisted(() => ({
  pickupTask: vi.fn(),
  replyToTask: vi.fn(),
  cancelTask: vi.fn(),
  retryTask: vi.fn(),
}));
const toasts = vi.hoisted(() => ({
  info: vi.fn(),
  success: vi.fn(),
  error: vi.fn(),
}));
const refresh = vi.hoisted(() => ({ fn: vi.fn() }));

vi.mock("@/auth", () => ({
  useAuth: () => ({ getToken: () => Promise.resolve("test-token") }),
}));
vi.mock("@/components/patterns/toast", () => ({ useToast: () => toasts }));
vi.mock("@/lib/hooks/use-sidebar-refresh", () => ({
  useSidebarRefresh: () => refresh.fn,
}));
vi.mock("@/lib/api/tasks-client", () => tasks);

function item(over: Partial<DigestItem> = {}): DigestItem {
  return {
    persona_id: "kai",
    title: "Book the dentist",
    detail: "",
    ref: { kind: "task", id: "task_1" },
    ran_because: null,
    actions: ["reply", "pickup", "cancel"],
    reason: "waiting",
    ...over,
  };
}

function ok(over: Record<string, unknown> = {}) {
  return {
    task_id: "task_1",
    status: "progressing",
    paused: false,
    changed: true,
    owner_autonomy_paused: false,
    note: "",
    ...over,
  };
}

function mount(over: Partial<DigestItem> = {}) {
  const onActed = vi.fn();
  const utils = render(
    <NextIntlClientProvider locale="en" messages={messages}>
      <AttentionActions item={item(over)} onActed={onActed} t={(key) => key} />
    </NextIntlClientProvider>,
  );
  return { ...utils, onActed };
}

const verb = (name: string) =>
  document.querySelector(`[data-verb="${name}"]`) as HTMLElement;

beforeEach(() => {
  vi.clearAllMocks();
  tasks.pickupTask.mockResolvedValue(ok());
  tasks.replyToTask.mockResolvedValue(ok());
  tasks.cancelTask.mockResolvedValue(ok({ status: "cancelled" }));
  tasks.retryTask.mockResolvedValue(ok({ successor_task_id: "task_2" }));
});

describe("which verbs a line offers", () => {
  it("offers exactly what the server said, in a stable order", () => {
    // The server decides from what the item IS (D-W1-6); the surface never invents a verb.
    expect(actionableVerbs(item({ actions: ["cancel", "reply"] }))).toEqual([
      "reply",
      "cancel",
    ]);
    expect(actionableVerbs(item({ actions: ["pickup", "cancel"] }))).toEqual([
      "pickup",
      "cancel",
    ]);
    expect(actionableVerbs(item({ actions: ["retry"] }))).toEqual(["retry"]);
  });

  it("acts on nothing for an approval line: that grant is made where it is visible", () => {
    const approval = item({
      ref: { kind: "approval", id: "p1" },
      actions: ["approve", "decline"],
      reason: "approval",
    });
    expect(isActionable(approval)).toBe(false);
    expect(actionableVerbs(approval)).toEqual([]);
    const { container } = render(
      <NextIntlClientProvider locale="en" messages={messages}>
        <AttentionActions item={approval} onActed={vi.fn()} t={(k) => k} />
      </NextIntlClientProvider>,
    );
    expect(container.firstChild).toBeNull();
  });

  it("acts on nothing when there is no task behind the line", () => {
    expect(isActionable(item({ ref: null }))).toBe(false);
  });

  it("still acts on nothing for an approval line that offers a task verb", () => {
    // The guard is on WHAT THE LINE IS, not on which verbs happen to be listed. Today the
    // server gives an approval item exactly approve/decline, so filtering by verb alone would
    // look equivalent; it is not. If a proposal line ever also offered, say, cancelling the
    // task behind it, verb-filtering alone would start granting from a one-line summary, which
    // is the thing D-W1-32 forbids. A mutation that swapped the ref check for a null check
    // survived until this case existed.
    const approvalWithTaskVerb = item({
      ref: { kind: "approval", id: "p1" },
      actions: ["approve", "decline", "cancel"],
      reason: "approval",
    });
    expect(actionableVerbs(approvalWithTaskVerb)).toEqual([]);
    expect(isActionable(approvalWithTaskVerb)).toBe(false);
  });

  it("ignores a verb it cannot carry out", () => {
    expect(actionableVerbs(item({ actions: ["approve", "pickup"] }))).toEqual([
      "pickup",
    ]);
  });
});

describe("the verbs act on the task behind the line", () => {
  it("picks up through the durable command, then re-reads and moves the badge", async () => {
    const { onActed } = mount({ actions: ["pickup", "cancel"] });
    fireEvent.click(verb("pickup"));
    await waitFor(() => expect(tasks.pickupTask).toHaveBeenCalledOnce());
    expect(tasks.pickupTask).toHaveBeenCalledWith("test-token", "task_1");
    expect(onActed).toHaveBeenCalledOnce(); // the list is re-read, never patched
    expect(refresh.fn).toHaveBeenCalledOnce(); // the badge is that list's length
  });

  it("carries the user's own words into the reply", async () => {
    mount();
    fireEvent.click(verb("reply"));
    const box = screen.getByRole("textbox");
    fireEvent.change(box, { target: { value: "  the 14th works  " } });
    fireEvent.click(verb("reply-send"));
    await waitFor(() => expect(tasks.replyToTask).toHaveBeenCalledOnce());
    expect(tasks.replyToTask).toHaveBeenCalledWith(
      "test-token",
      "task_1",
      "the 14th works",
    );
  });

  it("does not send an empty reply", async () => {
    mount();
    fireEvent.click(verb("reply"));
    fireEvent.change(screen.getByRole("textbox"), {
      target: { value: "   " },
    });
    expect((verb("reply-send") as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(verb("reply-send"));
    expect(tasks.replyToTask).not.toHaveBeenCalled();
  });

  it("stops the reply at the cap the route enforces (D-W1-7)", () => {
    mount();
    fireEvent.click(verb("reply"));
    expect(screen.getByRole("textbox").getAttribute("maxlength")).toBe("8000");
  });

  it("cancels and retries through their own commands", async () => {
    const { unmount } = mount({ actions: ["cancel"] });
    fireEvent.click(verb("cancel"));
    await waitFor(() => expect(tasks.cancelTask).toHaveBeenCalledOnce());
    unmount();

    mount({ actions: ["retry"] });
    fireEvent.click(verb("retry"));
    await waitFor(() => expect(tasks.retryTask).toHaveBeenCalledOnce());
  });
});

describe("a no-op is calm and honest", () => {
  it("says the server's own sentence when nothing changed", async () => {
    tasks.pickupTask.mockResolvedValue(
      ok({
        changed: false,
        note: "Your autonomy is paused, so it won't run yet.",
        owner_autonomy_paused: true,
      }),
    );
    const { onActed } = mount({ actions: ["pickup"] });
    fireEvent.click(verb("pickup"));
    await waitFor(() => expect(toasts.info).toHaveBeenCalledOnce());
    expect(toasts.info).toHaveBeenCalledWith(
      "Your autonomy is paused, so it won't run yet.",
    );
    expect(toasts.error).not.toHaveBeenCalled(); // a no-op is not a failure
    expect(onActed).toHaveBeenCalledOnce(); // and the truth is re-read either way
  });

  it("reports a real failure as a failure and leaves the list alone", async () => {
    tasks.pickupTask.mockRejectedValue(new Error("500"));
    const { onActed } = mount({ actions: ["pickup"] });
    fireEvent.click(verb("pickup"));
    await waitFor(() => expect(toasts.error).toHaveBeenCalledOnce());
    expect(onActed).not.toHaveBeenCalled();
  });
});
