/**
 * R9-012 — MeLiveRefresh: the live channel + window focus keep the
 * server-rendered sidebar honest.
 *
 * Locks: every subscribed event (message.delivered / notification.created /
 * task.updated / sidebar.changed / resync) drives ONE debounced
 * `router.refresh()` per burst (fake timers); task.updated ALSO bridges onto
 * the W8 task-signal bus; window focus is the fail-soft catch-up.
 */

import { act, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MeEventHandler } from "@/components/providers/me-events-provider";
import { SIDEBAR_REFRESH_DEBOUNCE_MS } from "@/lib/hooks/use-sidebar-refresh";
import { MeLiveRefresh } from "./me-live-refresh";

const refresh = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh }),
}));

// Capture useMeEvent subscriptions so the test can fire channel events.
const handlers = new Map<string, MeEventHandler[]>();
vi.mock("@/components/providers/me-events-provider", () => ({
  useMeEvent: (type: string, handler: MeEventHandler) => {
    const list = handlers.get(type) ?? [];
    list.push(handler);
    handlers.set(type, list);
  },
}));

const taskSignals: unknown[] = [];
vi.mock("@/lib/task-signal", () => ({
  emitTaskSignal: (signal: unknown) => taskSignals.push(signal),
}));

function fire(type: string, data: Record<string, unknown> = {}) {
  for (const h of handlers.get(type) ?? []) h(data);
}

beforeEach(() => {
  refresh.mockClear();
  handlers.clear();
  taskSignals.length = 0;
  vi.useFakeTimers();
});
afterEach(() => {
  vi.useRealTimers();
});

describe("MeLiveRefresh (R9-012)", () => {
  it("subscribes the full sidebar-moving event set", () => {
    render(<MeLiveRefresh />);
    for (const type of [
      "message.delivered",
      "notification.created",
      "task.updated",
      "sidebar.changed",
      "resync",
    ]) {
      expect(handlers.has(type), type).toBe(true);
    }
  });

  it.each([
    ["message.delivered"],
    ["notification.created"],
    ["sidebar.changed"],
    ["resync"],
  ])("%s → one debounced soft refresh", (type) => {
    render(<MeLiveRefresh />);
    act(() => fire(type));
    expect(refresh).not.toHaveBeenCalled(); // trailing debounce
    act(() => vi.advanceTimersByTime(SIDEBAR_REFRESH_DEBOUNCE_MS));
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("an event burst coalesces into ONE refresh", () => {
    render(<MeLiveRefresh />);
    act(() => {
      fire("task.updated", { task_id: "t1", state: "active" });
      fire("task.updated", { task_id: "t1", state: "completed" });
      fire("notification.created", { kind: "schedule_fired" });
      fire("sidebar.changed", { reason: "persona.created" });
    });
    act(() => vi.advanceTimersByTime(SIDEBAR_REFRESH_DEBOUNCE_MS));
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("task.updated also bridges onto the W8 task-signal bus (Spec A6)", () => {
    render(<MeLiveRefresh />);
    act(() => fire("task.updated", { task_id: "t42", state: "waiting" }));
    expect(taskSignals).toEqual([{ id: "task:1", taskId: "t42" }]);
    act(() => vi.advanceTimersByTime(SIDEBAR_REFRESH_DEBOUNCE_MS));
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("window focus is the fail-soft catch-up (channel down ⇒ still converges)", () => {
    render(<MeLiveRefresh />);
    act(() => {
      window.dispatchEvent(new Event("focus"));
    });
    act(() => vi.advanceTimersByTime(SIDEBAR_REFRESH_DEBOUNCE_MS));
    expect(refresh).toHaveBeenCalledTimes(1);
  });
});
