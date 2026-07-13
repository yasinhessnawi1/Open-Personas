"use client";

/**
 * R9-036 — press-and-hold-to-arm, drag-to-commit gesture.
 *
 * The reusable mechanic behind "hold a persona avatar to reveal a call/chat
 * live preview, drag up/down to choose, release past a threshold to commit."
 * This hook is generic over WHAT commits — callers supply `onCommitUp` /
 * `onCommitDown`; it only knows about hold-timing, drag distance, and the
 * armed/highlight state machine. Any future surface (not just the sidebar)
 * can reuse it for its own up/down pair — see `<PressSwipePreview>`
 * (components/patterns/press-swipe-preview.tsx) for the matching live-preview
 * chips this hook is designed to drive.
 *
 * Interaction contract (owner ruling, docs/specs/phase3/spec_R9/issues.md
 * R9-036):
 *   - press-and-hold ~250ms ARMS the gesture. The hold is what disambiguates
 *     a hold-to-drag from an ordinary scroll-drag (see "no-arm" below) —
 *     it's the only reason this needs a timer at all.
 *   - while armed, dragging vertically past `thresholdPx` highlights the
 *     corresponding direction; releasing while highlighted COMMITS that
 *     direction. An early release or a drag-back (magnitude under threshold
 *     at release) cancels cleanly — no commit, no navigation.
 *   - a plain tap (release before the hold timer fires) is UNCHANGED: this
 *     hook never calls `preventDefault` / `stopPropagation` on that path, so
 *     the element's native click (a Link's navigation) fires exactly as it
 *     always did.
 *   - an immediate drag (movement past `slopPx` BEFORE the hold timer fires)
 *     never arms — the hook detaches without touching the event, so native
 *     scrolling (e.g. the sidebar rail) is never hijacked.
 *   - once armed, the pointer's eventual synthetic `click` (a pointerdown +
 *     pointerup pair with ~no net movement fires a native click, the same as
 *     any tap) is suppressed via `handlers.onClick` — an armed-then-
 *     cancelled/committed gesture must never ALSO trigger the element's
 *     ordinary click-navigation.
 *
 * Implementation notes:
 *   - Pointer Events (not separate touch/mouse handlers) unify touch, mouse,
 *     and pen in one code path.
 *   - Move/up/cancel listen on `document` rather than using
 *     `setPointerCapture` — the same technique the sidebar's own
 *     resize-drag already uses (components/shell/sidebar.tsx's
 *     `startDrag`/`onPointerMove`/`stopDrag`), and one jsdom can simulate
 *     without a `setPointerCapture` polyfill (jsdom doesn't implement it).
 *   - Only one gesture is tracked at a time (by `pointerId`); a second
 *     concurrent pointer (e.g. a second finger) is ignored until the first
 *     one finishes.
 */

import { useCallback, useEffect, useRef, useState } from "react";

export type SwipeDirection = "up" | "down";

export interface PressSwipeGestureOptions {
  /** Fires when the gesture commits with "up" highlighted at release. */
  onCommitUp: () => void;
  /** Fires when the gesture commits with "down" highlighted at release. */
  onCommitDown: () => void;
  /**
   * Hold duration (ms) before the gesture arms. Default 250 — the owner's
   * ruling in R9-036 ("press-hold (~250ms) ARMS the gesture").
   */
  holdMs?: number;
  /**
   * Vertical drag distance (px) past which a direction highlights, and past
   * which a release commits it. Default 48 — the midpoint of the owner's
   * quoted 40-56px range: big enough that a shaky hold or a slightly
   * overshot tap doesn't accidentally commit, small enough to feel
   * responsive. It also roughly matches the `md` persona avatar's own
   * footprint (40px, see persona-avatar.tsx's SIZE_CLASSES), so the drag
   * reads as "about one avatar's height" — a distance the user is already
   * looking at, rounded to the app's 4px spacing scale.
   */
  thresholdPx?: number;
  /**
   * Pre-arm movement slop (px). Movement past this BEFORE the hold timer
   * fires cancels arming outright — the gesture never arms and nothing is
   * suppressed. This is what keeps ordinary scrolling untouched. Default 10
   * — a conventional small touch-slop, under the ~10-15px range browsers
   * themselves use to tell a tap from a pan.
   */
  slopPx?: number;
}

export interface PressSwipeGesture<T extends HTMLElement> {
  /** True once the hold has armed the gesture — render the live-preview chips. */
  armed: boolean;
  /** Which direction is currently past the commit threshold, or `null` (neutral). */
  highlight: SwipeDirection | null;
  /**
   * The armed element's viewport rect, captured the instant the gesture
   * arms — the anchor a floating preview positions against. `null` until
   * armed (and reset to `null` once the gesture ends).
   */
  anchorRect: DOMRect | null;
  /** Attach to the SAME interactive element `handlers` is spread onto. */
  elementRef: React.RefObject<T | null>;
  /** Spread onto the interactive element (a Link/anchor/button). */
  handlers: {
    onPointerDown: (event: React.PointerEvent<T>) => void;
    onClick: (event: React.MouseEvent<T>) => void;
  };
}

const DEFAULT_HOLD_MS = 250;
const DEFAULT_THRESHOLD_PX = 48;
const DEFAULT_SLOP_PX = 10;

export function usePressSwipeGesture<T extends HTMLElement = HTMLElement>({
  onCommitUp,
  onCommitDown,
  holdMs = DEFAULT_HOLD_MS,
  thresholdPx = DEFAULT_THRESHOLD_PX,
  slopPx = DEFAULT_SLOP_PX,
}: PressSwipeGestureOptions): PressSwipeGesture<T> {
  const [armed, setArmed] = useState(false);
  const [highlight, setHighlight] = useState<SwipeDirection | null>(null);
  const [anchorRect, setAnchorRect] = useState<DOMRect | null>(null);

  const elementRef = useRef<T | null>(null);
  const suppressClickRef = useRef(false);
  // Any in-flight gesture's teardown — a defensive unmount-time cleanup (the
  // sidebar's own live-refresh, R9-012, can re-render/unmount this list from
  // under an active hold).
  const detachRef = useRef<(() => void) | null>(null);

  useEffect(() => {
    return () => {
      detachRef.current?.();
    };
  }, []);

  const onPointerDown = useCallback(
    (event: React.PointerEvent<T>) => {
      // Ignore non-primary mouse buttons (right/middle click). Every touch
      // and pen contact reports button 0, so this only filters mouse.
      if (event.pointerType === "mouse" && event.button !== 0) return;
      // One gesture at a time — a second concurrent pointer (another finger)
      // is ignored until the first finishes.
      if (detachRef.current !== null) return;

      const pointerId = event.pointerId;
      const startX = event.clientX;
      const startY = event.clientY;
      let isArmed = false;

      let timer: ReturnType<typeof setTimeout> | null = setTimeout(() => {
        timer = null;
        isArmed = true;
        setAnchorRect(elementRef.current?.getBoundingClientRect() ?? null);
        setHighlight(null);
        setArmed(true);
      }, holdMs);

      const detach = () => {
        if (timer !== null) {
          clearTimeout(timer);
          timer = null;
        }
        document.removeEventListener("pointermove", handleMove);
        document.removeEventListener("pointerup", handleUp);
        document.removeEventListener("pointercancel", handleCancel);
        detachRef.current = null;
      };

      const finish = (commit: SwipeDirection | null) => {
        const wasArmed = isArmed;
        detach();
        isArmed = false;
        setArmed(false);
        setHighlight(null);
        setAnchorRect(null);
        if (wasArmed) {
          // The gesture reached "armed": its eventual click (the browser
          // fires one for this pointerdown/pointerup pair whenever the net
          // movement is small, same as any tap) must not ALSO navigate.
          suppressClickRef.current = true;
        }
        if (commit === "up") onCommitUp();
        else if (commit === "down") onCommitDown();
      };

      function handleMove(moveEvent: PointerEvent) {
        if (moveEvent.pointerId !== pointerId) return;
        const dy = moveEvent.clientY - startY;
        if (!isArmed) {
          // Pre-arm: real movement means the user is scrolling/dragging, not
          // holding. Abandon the timer and touch NOTHING else — no
          // preventDefault, no state change — so native scroll (and, for a
          // short drag-release, the native click) behaves exactly as if
          // this hook didn't exist. This is the whole "immediate drag
          // scrolls, never arms" guarantee.
          const dx = moveEvent.clientX - startX;
          if (Math.hypot(dx, dy) > slopPx) detach();
          return;
        }
        // Armed: this is the live-preview drag. Stop the page from ALSO
        // scrolling underneath the preview and update the highlight.
        moveEvent.preventDefault();
        if (dy <= -thresholdPx) setHighlight("up");
        else if (dy >= thresholdPx) setHighlight("down");
        else setHighlight(null);
      }

      function handleUp(upEvent: PointerEvent) {
        if (upEvent.pointerId !== pointerId) return;
        if (!isArmed) {
          // Released before the hold armed — a plain tap (or a released
          // micro-drag under slop). Detach quietly; the native click fires
          // unmolested.
          detach();
          return;
        }
        const dy = upEvent.clientY - startY;
        const commit: SwipeDirection | null =
          dy <= -thresholdPx ? "up" : dy >= thresholdPx ? "down" : null;
        finish(commit);
      }

      function handleCancel(cancelEvent: PointerEvent) {
        if (cancelEvent.pointerId !== pointerId) return;
        finish(null);
      }

      detachRef.current = detach;
      document.addEventListener("pointermove", handleMove);
      document.addEventListener("pointerup", handleUp);
      document.addEventListener("pointercancel", handleCancel);
    },
    [onCommitUp, onCommitDown, holdMs, thresholdPx, slopPx],
  );

  const onClick = useCallback((event: React.MouseEvent<T>) => {
    if (!suppressClickRef.current) return;
    suppressClickRef.current = false;
    event.preventDefault();
    event.stopPropagation();
  }, []);

  return {
    armed,
    highlight,
    anchorRect,
    elementRef,
    handlers: { onPointerDown, onClick },
  };
}
