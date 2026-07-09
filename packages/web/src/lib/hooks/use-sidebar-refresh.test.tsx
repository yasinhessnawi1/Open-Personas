/**
 * R9-012 — the shared sidebar-refresh seam.
 *
 * Locks the two contracts: the immediate seam maps 1:1 onto a soft
 * `router.refresh()` (a mutation → the server-rendered sidebar re-resolves),
 * and the debounced seam coalesces a burst into ONE trailing refresh (fake
 * timers), cancelling cleanly on unmount.
 */

import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  SIDEBAR_REFRESH_DEBOUNCE_MS,
  useDebouncedSidebarRefresh,
  useSidebarRefresh,
} from "./use-sidebar-refresh";

const refresh = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh }),
}));

beforeEach(() => {
  refresh.mockClear();
  vi.useFakeTimers();
});
afterEach(() => {
  vi.useRealTimers();
});

describe("useSidebarRefresh", () => {
  it("calls router.refresh immediately on each invocation (mutation seam)", () => {
    const { result } = renderHook(() => useSidebarRefresh());
    act(() => result.current());
    expect(refresh).toHaveBeenCalledTimes(1);
    act(() => result.current());
    expect(refresh).toHaveBeenCalledTimes(2);
  });
});

describe("useDebouncedSidebarRefresh", () => {
  it("coalesces a burst into one trailing refresh", () => {
    const { result } = renderHook(() => useDebouncedSidebarRefresh());
    act(() => {
      result.current();
      result.current();
      result.current();
    });
    expect(refresh).not.toHaveBeenCalled(); // trailing edge — nothing yet
    act(() => vi.advanceTimersByTime(SIDEBAR_REFRESH_DEBOUNCE_MS));
    expect(refresh).toHaveBeenCalledTimes(1); // the burst became ONE refresh
  });

  it("a fresh ping after the trailing fire schedules another refresh", () => {
    const { result } = renderHook(() => useDebouncedSidebarRefresh());
    act(() => result.current());
    act(() => vi.advanceTimersByTime(SIDEBAR_REFRESH_DEBOUNCE_MS));
    act(() => result.current());
    act(() => vi.advanceTimersByTime(SIDEBAR_REFRESH_DEBOUNCE_MS));
    expect(refresh).toHaveBeenCalledTimes(2);
  });

  it("cancels the pending refresh on unmount (no orphaned timer)", () => {
    const { result, unmount } = renderHook(() => useDebouncedSidebarRefresh());
    act(() => result.current());
    unmount();
    act(() => vi.advanceTimersByTime(SIDEBAR_REFRESH_DEBOUNCE_MS * 2));
    expect(refresh).not.toHaveBeenCalled();
  });
});
