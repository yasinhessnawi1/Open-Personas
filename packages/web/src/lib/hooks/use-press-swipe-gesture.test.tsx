/**
 * R9-036 — `usePressSwipeGesture` unit tests.
 *
 * A minimal harness component exercises the hook exactly the way
 * `PersonaRailItem` (components/shell/sidebar-sections.tsx) wires it: the
 * SAME `onClick` composition (`gesture.handlers.onClick(e)` then, only if
 * not suppressed, the caller's own tap handler) so "tap clicks through" /
 * "an armed release never also clicks through" prove the real contract, not
 * an implementation detail.
 *
 * `pointermove` / `pointerup` / `pointercancel` are dispatched on `document`
 * (not the element) because the hook listens there itself — the same
 * document-level-listener technique components/shell/sidebar.tsx's own
 * resize-drag already uses, chosen partly BECAUSE it's what jsdom can
 * simulate without a `setPointerCapture` polyfill (jsdom doesn't implement
 * pointer capture).
 */
import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  type PressSwipeGestureOptions,
  usePressSwipeGesture,
} from "./use-press-swipe-gesture";

const POINTER_ID = 7;

function Harness({
  onTap,
  ...options
}: PressSwipeGestureOptions & { onTap: () => void }) {
  const gesture = usePressSwipeGesture<HTMLButtonElement>(options);
  return (
    <div>
      <button
        ref={gesture.elementRef}
        type="button"
        data-testid="target"
        onPointerDown={gesture.handlers.onPointerDown}
        onClick={(event) => {
          gesture.handlers.onClick(event);
          if (!event.defaultPrevented) onTap();
        }}
      >
        target
      </button>
      <div data-testid="armed">{String(gesture.armed)}</div>
      <div data-testid="highlight">{String(gesture.highlight)}</div>
      <div data-testid="anchored">{String(gesture.anchorRect !== null)}</div>
    </div>
  );
}

function down(opts: Partial<Record<string, unknown>> = {}) {
  fireEvent.pointerDown(screen.getByTestId("target"), {
    clientX: 0,
    clientY: 0,
    pointerId: POINTER_ID,
    pointerType: "touch",
    button: 0,
    ...opts,
  });
}

function move(
  clientY: number,
  clientX = 0,
  opts: Record<string, unknown> = {},
) {
  fireEvent.pointerMove(document, {
    clientX,
    clientY,
    pointerId: POINTER_ID,
    ...opts,
  });
}

function up(clientY = 0, clientX = 0, opts: Record<string, unknown> = {}) {
  fireEvent.pointerUp(document, {
    clientX,
    clientY,
    pointerId: POINTER_ID,
    ...opts,
  });
}

function click() {
  fireEvent.click(screen.getByTestId("target"));
}

function armHold(holdMs = 250) {
  // The hold timer's callback calls React state setters outside of any
  // `fireEvent` (which wraps its dispatch in `act` automatically) — wrap it
  // explicitly so the resulting re-render is flushed before the next
  // assertion, regardless of whether a later `fireEvent` call would have
  // incidentally flushed it too.
  act(() => {
    vi.advanceTimersByTime(holdMs);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  // jsdom never lays anything out; stub a stable rect so `anchorRect` (read
  // at arm-time) is populated deterministically.
  HTMLElement.prototype.getBoundingClientRect = vi.fn(() => ({
    top: 100,
    bottom: 140,
    left: 20,
    right: 60,
    width: 40,
    height: 40,
    x: 20,
    y: 100,
    toJSON() {
      return this;
    },
  }));
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("usePressSwipeGesture", () => {
  it("arms after the hold duration", () => {
    const onCommitUp = vi.fn();
    const onCommitDown = vi.fn();
    const onTap = vi.fn();
    render(
      <Harness
        onCommitUp={onCommitUp}
        onCommitDown={onCommitDown}
        onTap={onTap}
      />,
    );

    down();
    expect(screen.getByTestId("armed")).toHaveTextContent("false");
    armHold();
    expect(screen.getByTestId("armed")).toHaveTextContent("true");
    // The anchor rect is captured the instant it arms.
    expect(screen.getByTestId("anchored")).toHaveTextContent("true");
  });

  it("never arms when the pointer moves past the slop before the hold fires (the scroll case)", () => {
    const onCommitUp = vi.fn();
    const onCommitDown = vi.fn();
    const onTap = vi.fn();
    render(
      <Harness
        onCommitUp={onCommitUp}
        onCommitDown={onCommitDown}
        onTap={onTap}
      />,
    );

    down();
    // A real scroll-drag: movement well past the default 10px slop, before
    // the 250ms hold elapses.
    move(30);
    armHold();
    expect(screen.getByTestId("armed")).toHaveTextContent("false");
    // Releasing afterwards does not commit anything — this pointer sequence
    // was abandoned to the (simulated) native scroll, not treated as a tap
    // either (real movement occurred).
    up(30);
    expect(onCommitUp).not.toHaveBeenCalled();
    expect(onCommitDown).not.toHaveBeenCalled();
  });

  it("commits 'up' (the call action) when released past the threshold while dragged up", () => {
    const onCommitUp = vi.fn();
    const onCommitDown = vi.fn();
    const onTap = vi.fn();
    render(
      <Harness
        onCommitUp={onCommitUp}
        onCommitDown={onCommitDown}
        onTap={onTap}
      />,
    );

    down();
    armHold();
    move(-60); // past the default 48px threshold, upward
    expect(screen.getByTestId("highlight")).toHaveTextContent("up");
    up(-60);
    expect(onCommitUp).toHaveBeenCalledTimes(1);
    expect(onCommitDown).not.toHaveBeenCalled();
    expect(screen.getByTestId("armed")).toHaveTextContent("false");
  });

  it("commits 'down' (the chat action) when released past the threshold while dragged down", () => {
    const onCommitUp = vi.fn();
    const onCommitDown = vi.fn();
    const onTap = vi.fn();
    render(
      <Harness
        onCommitUp={onCommitUp}
        onCommitDown={onCommitDown}
        onTap={onTap}
      />,
    );

    down();
    armHold();
    move(60);
    expect(screen.getByTestId("highlight")).toHaveTextContent("down");
    up(60);
    expect(onCommitDown).toHaveBeenCalledTimes(1);
    expect(onCommitUp).not.toHaveBeenCalled();
  });

  it("an early release (armed, under threshold) cancels cleanly — no commit, no click-through", () => {
    const onCommitUp = vi.fn();
    const onCommitDown = vi.fn();
    const onTap = vi.fn();
    render(
      <Harness
        onCommitUp={onCommitUp}
        onCommitDown={onCommitDown}
        onTap={onTap}
      />,
    );

    down();
    armHold();
    up(0); // released with ~no movement, well under the 48px threshold
    expect(onCommitUp).not.toHaveBeenCalled();
    expect(onCommitDown).not.toHaveBeenCalled();
    expect(screen.getByTestId("armed")).toHaveTextContent("false");
    // The browser fires a synthetic click for this pointerdown/pointerup
    // pair (near-zero net movement) — it must be suppressed, not fall
    // through to the tap handler (which would be the Link's navigation in
    // the real component).
    click();
    expect(onTap).not.toHaveBeenCalled();
  });

  it("a drag-back below the threshold before release cancels (no commit)", () => {
    const onCommitUp = vi.fn();
    const onCommitDown = vi.fn();
    const onTap = vi.fn();
    render(
      <Harness
        onCommitUp={onCommitUp}
        onCommitDown={onCommitDown}
        onTap={onTap}
      />,
    );

    down();
    armHold();
    move(-60); // past threshold, up
    expect(screen.getByTestId("highlight")).toHaveTextContent("up");
    move(-5); // dragged back toward center, under threshold again
    expect(screen.getByTestId("highlight")).toHaveTextContent("null");
    up(-5);
    expect(onCommitUp).not.toHaveBeenCalled();
    expect(onCommitDown).not.toHaveBeenCalled();
  });

  it("a plain tap (release before the hold fires) clicks through unmolested", () => {
    const onCommitUp = vi.fn();
    const onCommitDown = vi.fn();
    const onTap = vi.fn();
    render(
      <Harness
        onCommitUp={onCommitUp}
        onCommitDown={onCommitDown}
        onTap={onTap}
      />,
    );

    down();
    up(0); // released well before the 250ms hold
    click();
    expect(onTap).toHaveBeenCalledTimes(1);
    expect(onCommitUp).not.toHaveBeenCalled();
    expect(onCommitDown).not.toHaveBeenCalled();
    expect(screen.getByTestId("armed")).toHaveTextContent("false");
  });

  it("a pointercancel while armed cancels cleanly (no commit)", () => {
    const onCommitUp = vi.fn();
    const onCommitDown = vi.fn();
    const onTap = vi.fn();
    render(
      <Harness
        onCommitUp={onCommitUp}
        onCommitDown={onCommitDown}
        onTap={onTap}
      />,
    );

    down();
    armHold();
    fireEvent.pointerCancel(document, { pointerId: POINTER_ID });
    expect(screen.getByTestId("armed")).toHaveTextContent("false");
    expect(onCommitUp).not.toHaveBeenCalled();
    expect(onCommitDown).not.toHaveBeenCalled();
  });

  it("ignores a non-primary mouse button (right-click never arms)", () => {
    const onCommitUp = vi.fn();
    const onCommitDown = vi.fn();
    const onTap = vi.fn();
    render(
      <Harness
        onCommitUp={onCommitUp}
        onCommitDown={onCommitDown}
        onTap={onTap}
      />,
    );

    down({ pointerType: "mouse", button: 2 });
    armHold();
    expect(screen.getByTestId("armed")).toHaveTextContent("false");
  });

  it("ignores a second concurrent pointer while a gesture is in flight", () => {
    const onCommitUp = vi.fn();
    const onCommitDown = vi.fn();
    const onTap = vi.fn();
    render(
      <Harness
        onCommitUp={onCommitUp}
        onCommitDown={onCommitDown}
        onTap={onTap}
      />,
    );

    down({ pointerId: POINTER_ID });
    // A second finger touches down on the same element before the first
    // lifts — must not start a second overlapping gesture/timer.
    down({ pointerId: POINTER_ID + 1, clientY: 0 });
    armHold();
    // Only ONE gesture armed (the first pointer's) — release the SECOND
    // pointer's id and confirm it does nothing (it was never tracked).
    fireEvent.pointerUp(document, {
      clientY: 60,
      pointerId: POINTER_ID + 1,
    });
    expect(onCommitDown).not.toHaveBeenCalled();
    // The first pointer's own release still works normally.
    up(60, 0, { pointerId: POINTER_ID });
    expect(onCommitDown).toHaveBeenCalledTimes(1);
  });

  it("respects custom holdMs / thresholdPx", () => {
    const onCommitUp = vi.fn();
    const onCommitDown = vi.fn();
    const onTap = vi.fn();
    render(
      <Harness
        onCommitUp={onCommitUp}
        onCommitDown={onCommitDown}
        onTap={onTap}
        holdMs={100}
        thresholdPx={20}
      />,
    );

    down();
    act(() => {
      vi.advanceTimersByTime(99);
    });
    expect(screen.getByTestId("armed")).toHaveTextContent("false");
    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(screen.getByTestId("armed")).toHaveTextContent("true");
    move(25);
    expect(screen.getByTestId("highlight")).toHaveTextContent("down");
    up(25);
    expect(onCommitDown).toHaveBeenCalledTimes(1);
  });

  it("cleans up an in-flight gesture's document listeners on unmount (no leaked commits)", () => {
    const onCommitUp = vi.fn();
    const onCommitDown = vi.fn();
    const onTap = vi.fn();
    const { unmount } = render(
      <Harness
        onCommitUp={onCommitUp}
        onCommitDown={onCommitDown}
        onTap={onTap}
      />,
    );

    down();
    armHold();
    unmount();
    // If cleanup failed, these would still be wired to the (now-detached)
    // handlers' closures — dispatching them must be a silent no-op.
    move(60);
    up(60);
    expect(onCommitUp).not.toHaveBeenCalled();
    expect(onCommitDown).not.toHaveBeenCalled();
  });
});
