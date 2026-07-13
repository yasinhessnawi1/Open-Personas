/**
 * R9-036 REOPEN — `usePressSwipeGesture` unit tests (real swipe, no hold;
 * circle-radius commit geometry).
 *
 * A minimal harness component exercises the hook exactly the way
 * `PersonaRailItem` (components/shell/sidebar-sections.tsx) wires it: the
 * SAME `onClick` composition (`gesture.handlers.onClick(e)` then, only if
 * not suppressed, the caller's own tap handler) so "tap clicks through" /
 * "a swiped release never also clicks through" prove the real contract, not
 * an implementation detail.
 *
 * `pointermove` / `pointerup` / `pointercancel` are dispatched on `document`
 * (not the element) because the hook listens there itself — the same
 * document-level-listener technique components/shell/sidebar.tsx's own
 * resize-drag already uses, chosen partly BECAUSE it's what jsdom can
 * simulate without a `setPointerCapture` polyfill (jsdom doesn't implement
 * pointer capture).
 *
 * The stubbed `getBoundingClientRect()` below is a 40x40 square centered at
 * (40, 120) — i.e. top:100/bottom:140/left:20/right:60 — so its own radius
 * (half the side) is exactly 20px. Every test presses down at the rect's
 * OWN center (clientX:40, clientY:120) so "distance from press position"
 * and "distance from the circle's center" coincide, keeping the arithmetic
 * legible: a move to clientY:145 is +25 from center, past the 20px radius.
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  type PressSwipeGestureOptions,
  usePressSwipeGesture,
} from "./use-press-swipe-gesture";

const POINTER_ID = 7;
const CENTER_X = 40;
const CENTER_Y = 120;
const RADIUS = 20;

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
        // Spread first (matches the real call site, sidebar-sections.tsx's
        // PersonaRailItem) so onClick below still overrides the spread's own
        // onClick, exactly like the production wiring.
        {...gesture.handlers}
        onClick={(event) => {
          gesture.handlers.onClick(event);
          if (!event.defaultPrevented) onTap();
        }}
      >
        target
      </button>
      <div data-testid="armed">{String(gesture.armed)}</div>
      <div data-testid="highlight">{String(gesture.highlight)}</div>
      <div data-testid="progress">{gesture.progress}</div>
      <div data-testid="anchored">{String(gesture.anchorRect !== null)}</div>
    </div>
  );
}

function down(clientY = CENTER_Y, opts: Record<string, unknown> = {}) {
  fireEvent.pointerDown(screen.getByTestId("target"), {
    clientX: CENTER_X,
    clientY,
    pointerId: POINTER_ID,
    pointerType: "touch",
    button: 0,
    ...opts,
  });
}

function move(clientY: number, opts: Record<string, unknown> = {}) {
  fireEvent.pointerMove(document, {
    clientX: CENTER_X,
    clientY,
    pointerId: POINTER_ID,
    ...opts,
  });
}

function up(clientY: number, opts: Record<string, unknown> = {}) {
  fireEvent.pointerUp(document, {
    clientX: CENTER_X,
    clientY,
    pointerId: POINTER_ID,
    ...opts,
  });
}

function click() {
  fireEvent.click(screen.getByTestId("target"));
}

beforeEach(() => {
  // jsdom never lays anything out; stub a stable 40x40 rect (centered at
  // 40,120, radius 20) so the geometry-based commit threshold is
  // deterministic. See the file-level comment for the exact numbers.
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
  vi.restoreAllMocks();
});

describe("usePressSwipeGesture", () => {
  it("a plain tap (release under slop) clicks through unmolested — no timer, no commit", () => {
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
    up(CENTER_Y + 4); // well under the default 8px slop, no direction lock
    expect(screen.getByTestId("armed")).toHaveTextContent("false");
    click();
    expect(onTap).toHaveBeenCalledTimes(1);
    expect(onCommitUp).not.toHaveBeenCalled();
    expect(onCommitDown).not.toHaveBeenCalled();
  });

  it("vertical movement past the tap-slop immediately shows the preview — no hold, no timer", () => {
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
    move(CENTER_Y + 10); // past the default 8px slop
    expect(screen.getByTestId("armed")).toHaveTextContent("true");
    // The anchor rect is captured the instant swiping starts.
    expect(screen.getByTestId("anchored")).toHaveTextContent("true");
    // 10px from center is inside the 20px radius — not locked yet.
    expect(screen.getByTestId("highlight")).toHaveTextContent("null");
  });

  it("crossing the circle's rim upward locks 'up' (the call action)", () => {
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
    move(CENTER_Y - RADIUS - 5); // 25px above center, past the 20px radius
    expect(screen.getByTestId("highlight")).toHaveTextContent("up");
    expect(screen.getByTestId("progress")).toHaveTextContent("-1");
    up(CENTER_Y - RADIUS - 5);
    expect(onCommitUp).toHaveBeenCalledTimes(1);
    expect(onCommitDown).not.toHaveBeenCalled();
    expect(screen.getByTestId("armed")).toHaveTextContent("false");
  });

  it("crossing the circle's rim downward locks 'down' (the chat action)", () => {
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
    move(CENTER_Y + RADIUS + 5); // 25px below center, past the 20px radius
    expect(screen.getByTestId("highlight")).toHaveTextContent("down");
    expect(screen.getByTestId("progress")).toHaveTextContent("1");
    up(CENTER_Y + RADIUS + 5);
    expect(onCommitDown).toHaveBeenCalledTimes(1);
    expect(onCommitUp).not.toHaveBeenCalled();
  });

  it("dragging back inside the circle unlocks (no commit on release)", () => {
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
    move(CENTER_Y - RADIUS - 5); // locks "up"
    expect(screen.getByTestId("highlight")).toHaveTextContent("up");
    move(CENTER_Y - 5); // back inside the circle (5px from center)
    expect(screen.getByTestId("highlight")).toHaveTextContent("null");
    up(CENTER_Y - 5);
    expect(onCommitUp).not.toHaveBeenCalled();
    expect(onCommitDown).not.toHaveBeenCalled();
  });

  it("release while swiping but unlocked (inside the circle) cancels — no commit, click suppressed", () => {
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
    move(CENTER_Y + 10); // past slop, inside the 20px radius — unlocked
    up(CENTER_Y + 10);
    expect(onCommitUp).not.toHaveBeenCalled();
    expect(onCommitDown).not.toHaveBeenCalled();
    expect(screen.getByTestId("armed")).toHaveTextContent("false");
    // The browser fires a synthetic click for this pointerdown/pointerup
    // pair — it must be suppressed, not fall through to the tap handler
    // (the Link's navigation in the real component), because real vertical
    // movement occurred.
    click();
    expect(onTap).not.toHaveBeenCalled();
  });

  it("measures the commit radius from the pressed element's own rect by default", () => {
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
    // 9px from center: past the 8px tap-slop (swiping starts) but inside
    // the rect-measured 20px radius — must NOT lock.
    move(CENTER_Y + 9);
    expect(screen.getByTestId("armed")).toHaveTextContent("true");
    expect(screen.getByTestId("highlight")).toHaveTextContent("null");
  });

  it("a radiusPx override replaces the measured rect radius", () => {
    const onCommitUp = vi.fn();
    const onCommitDown = vi.fn();
    const onTap = vi.fn();
    render(
      <Harness
        onCommitUp={onCommitUp}
        onCommitDown={onCommitDown}
        onTap={onTap}
        radiusPx={5}
      />,
    );

    down();
    // Same 9px-from-center move as the default-radius test above, but with
    // radiusPx=5 this now DOES lock — proving the override, not the
    // measured 20px rect radius, drove the decision.
    move(CENTER_Y + 9);
    expect(screen.getByTestId("highlight")).toHaveTextContent("down");
    up(CENTER_Y + 9);
    expect(onCommitDown).toHaveBeenCalledTimes(1);
  });

  it("respects a custom slopPx", () => {
    const onCommitUp = vi.fn();
    const onCommitDown = vi.fn();
    const onTap = vi.fn();
    render(
      <Harness
        onCommitUp={onCommitUp}
        onCommitDown={onCommitDown}
        onTap={onTap}
        slopPx={15}
      />,
    );

    down();
    move(CENTER_Y + 10); // past the default 8px slop, under the custom 15px
    expect(screen.getByTestId("armed")).toHaveTextContent("false");
    move(CENTER_Y + 16); // past the custom 15px slop
    expect(screen.getByTestId("armed")).toHaveTextContent("true");
  });

  it("a pointercancel while swiping cancels cleanly (no commit)", () => {
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
    move(CENTER_Y + RADIUS + 5);
    fireEvent.pointerCancel(document, { pointerId: POINTER_ID });
    expect(screen.getByTestId("armed")).toHaveTextContent("false");
    expect(onCommitUp).not.toHaveBeenCalled();
    expect(onCommitDown).not.toHaveBeenCalled();
  });

  it("ignores a non-primary mouse button (right-click never swipes)", () => {
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

    down(CENTER_Y, { pointerType: "mouse", button: 2 });
    move(CENTER_Y + RADIUS + 5);
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

    down(CENTER_Y, { pointerId: POINTER_ID });
    // A second finger touches down on the same element before the first
    // lifts — must not start a second overlapping gesture.
    fireEvent.pointerDown(screen.getByTestId("target"), {
      clientX: CENTER_X,
      clientY: CENTER_Y,
      pointerId: POINTER_ID + 1,
      pointerType: "touch",
      button: 0,
    });
    // The second pointer's own move/release must do nothing (never tracked).
    fireEvent.pointerMove(document, {
      clientX: CENTER_X,
      clientY: CENTER_Y + RADIUS + 5,
      pointerId: POINTER_ID + 1,
    });
    fireEvent.pointerUp(document, {
      clientX: CENTER_X,
      clientY: CENTER_Y + RADIUS + 5,
      pointerId: POINTER_ID + 1,
    });
    expect(onCommitDown).not.toHaveBeenCalled();
    // The first pointer's own gesture still works normally.
    move(CENTER_Y + RADIUS + 5);
    up(CENTER_Y + RADIUS + 5);
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
    move(CENTER_Y + RADIUS + 5);
    unmount();
    // If cleanup failed, these would still be wired to the (now-detached)
    // handlers' closures — dispatching them must be a silent no-op.
    move(CENTER_Y + RADIUS + 5);
    up(CENTER_Y + RADIUS + 5);
    expect(onCommitUp).not.toHaveBeenCalled();
    expect(onCommitDown).not.toHaveBeenCalled();
  });

  // Real-browser native-drag guard (R9-036 REOPEN #2). jsdom has no native
  // drag-and-drop, so nothing above this line could ever have caught the
  // bug these two tests pin: a[href]/img are draggable BY DEFAULT in every
  // real browser, a press+move starts native drag instead of this hook's
  // own tracking, and the browser fires `pointercancel` before the slop is
  // even exceeded — the gesture died silently (no chips, no commit) while
  // every test above still passed. Verified live in a real browser
  // (R9-036-fix.md); these two tests are the regression pin.
  it("the bound element carries draggable=false, so a real browser never starts native drag on it", () => {
    render(
      <Harness onCommitUp={vi.fn()} onCommitDown={vi.fn()} onTap={vi.fn()} />,
    );
    expect(screen.getByTestId("target")).toHaveAttribute("draggable", "false");
  });

  it("a dragstart on the bound element is prevented, and an in-flight gesture keeps tracking normally", () => {
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
    move(CENTER_Y + 10); // past slop — swiping, matches the moment a real
    // browser would otherwise start native drag-and-drop on this element.
    expect(screen.getByTestId("armed")).toHaveTextContent("true");

    // `fireEvent.X` returns the raw `element.dispatchEvent(event)` result:
    // `false` iff some handler called `preventDefault()` on a cancelable
    // event — the direct, DOM-level proof `onDragStart` cancels it.
    const notPrevented = fireEvent.dragStart(screen.getByTestId("target"));
    expect(notPrevented).toBe(false);

    // The already-in-flight pointer gesture is untouched by the dragstart —
    // it keeps tracking and still commits normally on release past the rim.
    move(CENTER_Y + RADIUS + 5);
    expect(screen.getByTestId("highlight")).toHaveTextContent("down");
    up(CENTER_Y + RADIUS + 5);
    expect(onCommitDown).toHaveBeenCalledTimes(1);
    expect(onCommitUp).not.toHaveBeenCalled();
  });
});
