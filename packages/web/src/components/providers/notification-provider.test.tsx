/**
 * Spec 35 cluster L (D-35-10 / D-35-11) — NotificationProvider / useNotify.
 *
 * Verifies the façade contract: every notify() surfaces a sonner toast (mocked);
 * consequential levels (error/success) persist into the bell feed + bump the
 * unread count; transient info/warning don't persist unless opted in; markAllRead
 * clears unread; clear empties; the feed round-trips through localStorage + is
 * capped at FEED_CAP.
 */

import { act, render, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const toastFns = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
  info: vi.fn(),
  warning: vi.fn(),
  loading: vi.fn(() => "auto-id-1"),
}));
vi.mock("@/components/patterns/toast", () => ({ toast: toastFns }));

import {
  FEED_CAP,
  NotificationProvider,
  useNotify,
} from "./notification-provider";

function wrapper({ children }: { children: ReactNode }) {
  return <NotificationProvider>{children}</NotificationProvider>;
}

beforeEach(() => {
  window.localStorage.clear();
  vi.clearAllMocks();
});

describe("NotificationProvider / useNotify", () => {
  it("surfaces a toast for every level + persists consequential ones", () => {
    const { result } = renderHook(() => useNotify(), { wrapper });

    act(() => result.current.notify({ level: "success", title: "Saved" }));
    expect(toastFns.success).toHaveBeenCalledWith("Saved", undefined);
    expect(result.current.entries).toHaveLength(1);
    expect(result.current.unreadCount).toBe(1);

    act(() =>
      result.current.notify({ level: "error", title: "Failed", body: "x" }),
    );
    expect(toastFns.error).toHaveBeenCalledWith("Failed", { description: "x" });
    expect(result.current.entries).toHaveLength(2);
    expect(result.current.unreadCount).toBe(2);
    // Newest first.
    expect(result.current.entries[0].title).toBe("Failed");
  });

  it("does NOT persist transient info/warning unless opted in", () => {
    const { result } = renderHook(() => useNotify(), { wrapper });

    act(() => result.current.notify({ level: "info", title: "FYI" }));
    expect(toastFns.info).toHaveBeenCalled();
    expect(result.current.entries).toHaveLength(0);

    // Opt-in: a low-balance warning persists by passing persist: true.
    act(() =>
      result.current.notify({
        level: "warning",
        title: "Low balance",
        persist: true,
      }),
    );
    expect(result.current.entries).toHaveLength(1);
  });

  it("markAllRead zeroes the unread count; clear empties the feed", () => {
    const { result } = renderHook(() => useNotify(), { wrapper });
    act(() => result.current.notify({ level: "success", title: "A" }));
    act(() => result.current.notify({ level: "success", title: "B" }));
    expect(result.current.unreadCount).toBe(2);

    act(() => result.current.markAllRead());
    expect(result.current.unreadCount).toBe(0);
    expect(result.current.entries).toHaveLength(2);

    act(() => result.current.clear());
    expect(result.current.entries).toHaveLength(0);
  });

  it("caps the feed at FEED_CAP (newest kept)", () => {
    const { result } = renderHook(() => useNotify(), { wrapper });
    act(() => {
      for (let i = 0; i < FEED_CAP + 5; i++) {
        result.current.notify({ level: "success", title: `n${i}` });
      }
    });
    expect(result.current.entries).toHaveLength(FEED_CAP);
    expect(result.current.entries[0].title).toBe(`n${FEED_CAP + 4}`);
  });

  it("round-trips the feed through localStorage", () => {
    const first = renderHook(() => useNotify(), { wrapper });
    act(() =>
      first.result.current.notify({ level: "error", title: "Persisted" }),
    );
    first.unmount();

    // A fresh provider hydrates the same feed from localStorage on mount.
    const second = renderHook(() => useNotify(), { wrapper });
    expect(second.result.current.entries).toHaveLength(1);
    expect(second.result.current.entries[0].title).toBe("Persisted");
  });

  it("persists an optional href (deep-link) on the entry", () => {
    const { result } = renderHook(() => useNotify(), { wrapper });
    act(() =>
      result.current.notify({
        level: "success",
        title: "Task finished",
        href: "/runs/abc",
      }),
    );
    expect(result.current.entries[0].href).toBe("/runs/abc");
    // href round-trips through localStorage too.
    const reload = renderHook(() => useNotify(), { wrapper });
    expect(reload.result.current.entries[0].href).toBe("/runs/abc");
  });

  it("markRead marks a single entry read without clearing the rest", () => {
    const { result } = renderHook(() => useNotify(), { wrapper });
    act(() => result.current.notify({ level: "success", title: "A" }));
    act(() => result.current.notify({ level: "success", title: "B" }));
    const target = result.current.entries[0].id; // newest ("B")
    act(() => result.current.markRead(target));
    expect(result.current.unreadCount).toBe(1);
    expect(result.current.entries.find((e) => e.id === target)?.read).toBe(
      true,
    );
    expect(result.current.entries.find((e) => e.title === "A")?.read).toBe(
      false,
    );
  });

  it("R9-050: notifyLoading surfaces a sonner loading toast + returns its id, without touching the bell", () => {
    const { result } = renderHook(() => useNotify(), { wrapper });

    let toastId: string | number = "";
    act(() => {
      toastId = result.current.notifyLoading("Creating your file…");
    });

    expect(toastFns.loading).toHaveBeenCalledWith(
      "Creating your file…",
      undefined,
    );
    expect(toastId).toBe("auto-id-1"); // the id sonner assigned
    expect(result.current.entries).toHaveLength(0); // no outcome yet — nothing to persist
  });

  it("R9-050: notify({ id }) updates the SAME toast in place instead of stacking a new one", () => {
    const { result } = renderHook(() => useNotify(), { wrapper });

    let toastId: string | number = "";
    act(() => {
      toastId = result.current.notifyLoading("Creating your file…");
    });
    act(() =>
      result.current.notify({
        level: "success",
        title: "File created",
        id: toastId,
      }),
    );

    expect(toastFns.success).toHaveBeenCalledWith("File created", {
      id: toastId,
    });
    // The resolved outcome IS consequential — persists exactly once.
    expect(result.current.entries).toHaveLength(1);
    expect(result.current.entries[0].title).toBe("File created");
  });

  it("throws when used outside a NotificationProvider", () => {
    function Bare() {
      useNotify();
      return null;
    }
    expect(() => render(<Bare />)).toThrow(/NotificationProvider/);
  });
});
