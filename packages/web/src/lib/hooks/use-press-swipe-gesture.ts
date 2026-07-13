"use client";

/**
 * R9-036 REOPEN — real swipe, circle-radius commit geometry.
 *
 * The reusable mechanic behind "swipe a persona avatar up/down to call or
 * chat, with a live preview of the pending action." This hook is generic
 * over WHAT commits — callers supply `onCommitUp` / `onCommitDown`; it only
 * knows about slop, the pressed element's own circular geometry, and the
 * swiping/locked state machine. Any future surface (not just the sidebar)
 * can reuse it for its own up/down pair — see `<PressSwipePreview>`
 * (components/patterns/press-swipe-preview.tsx) for the matching live-preview
 * chips this hook is designed to drive.
 *
 * Interaction contract (owner ruling, docs/specs/phase3/spec_R9/issues.md
 * "R9-036 REOPEN" — supersedes the original R9-036 hold-to-arm design):
 *   - NO hold-to-arm and NO timer of any kind. pointer-down starts tracking
 *     immediately; once the pointer's VERTICAL displacement from the
 *     press position exceeds `slopPx` (default ~8px — just enough to keep an
 *     ordinary tap's hand-tremor from misfiring), the gesture enters swiping
 *     mode and the live-preview chips appear.
 *   - the commit geometry is the PRESSED ELEMENT'S OWN CIRCLE: at
 *     pointer-down we measure the element's rendered rect
 *     (`getBoundingClientRect()`) and take its radius (half the smaller
 *     side — the avatar is a circle, so width === height in practice, but
 *     `min()` keeps this sane for any bound element). While swiping, the
 *     pointer's vertical distance from the circle's own center (not the
 *     press position — the owner's ruling is explicitly about the CIRCLE'S
 *     rim, "halve the avatar circle") is compared against that radius:
 *     crossing above the rim LOCKS "up" (call), crossing below LOCKS "down"
 *     (chat). Dragging back inside the circle unlocks — this is fully
 *     continuous, recomputed on every move, not a one-shot latch.
 *   - release while locked COMMITS that direction. Release while swiping but
 *     unlocked (inside the circle) CANCELS cleanly — no commit, no
 *     navigation.
 *   - a plain tap (release before slop is exceeded) is UNCHANGED: this hook
 *     never calls `preventDefault` / `stopPropagation` on that path, so the
 *     element's native click (a Link's navigation) fires exactly as it
 *     always did.
 *   - once swiping starts, the pointer's eventual synthetic `click` (a
 *     pointerdown + pointerup pair with ~no net movement fires a native
 *     click, the same as any tap) is suppressed via `handlers.onClick` — a
 *     swiped-then-cancelled/committed gesture must never ALSO trigger the
 *     element's ordinary click-navigation.
 *
 * Scroll trade-off (owner-ruled, R9-036 REOPEN point 4): avatar-initiated
 * vertical drags belong ENTIRELY to this gesture now — there is no more
 * "immediate drag scrolls instead" escape hatch (that was the hold-era
 * guard; R9-036 REOPEN explicitly removes it). Callers apply `touch-action:
 * none` to the bound element UNCONDITIONALLY (not gated on any state from
 * this hook) so a touch drag starting on the avatar never triggers the
 * browser's native scroll in the first place; the surrounding strip still
 * scrolls normally from non-avatar space and always via wheel (wheel
 * scrolling is never touch-action-gated).
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
  /** Fires when the gesture commits with "up" locked at release. */
  onCommitUp: () => void;
  /** Fires when the gesture commits with "down" locked at release. */
  onCommitDown: () => void;
  /**
   * Vertical movement (px) from the press position past which the gesture
   * leaves "maybe a tap" and enters swiping mode (showing the live preview).
   * Default 8 — the owner's ruling in R9-036 REOPEN ("small ~8px slop to
   * keep taps clean").
   */
  slopPx?: number;
  /**
   * Override for the commit-lock radius (px). Defaults to HALF the pressed
   * element's own rendered size — measured via `getBoundingClientRect()` at
   * press time — which is the owner's explicit ruling ("the avatar circle
   * should be halved"; the threshold is the circle's own geometry, not a
   * hardcoded constant). This override exists for callers (tests, or a
   * future non-circular surface) that can't or don't want to rely on real
   * layout.
   */
  radiusPx?: number;
}

export interface PressSwipeGesture<T extends HTMLElement> {
  /** True once past the tap-slop — a real swipe is in progress; render the live-preview chips. */
  armed: boolean;
  /** Which direction is currently locked (past the circle's rim), or `null` (inside the circle). */
  highlight: SwipeDirection | null;
  /**
   * Signed, continuous progress toward each lock, clamped to [-1, 1]:
   * -1 = "up" locked (or beyond), +1 = "down" locked (or beyond), 0 =
   * centered on the circle. Only meaningful while `armed`; 0 otherwise.
   * Drives the live preview's approach animation (brighten/scale toward a
   * direction as the pointer nears its rim) — `highlight` alone is only the
   * discrete locked/unlocked edge.
   */
  progress: number;
  /**
   * The armed element's viewport rect, captured the instant the gesture
   * starts swiping (slop exceeded) — the anchor a floating preview positions
   * against. `null` until swiping (and reset to `null` once the gesture ends).
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

const DEFAULT_SLOP_PX = 8;
/**
 * Fallback commit radius when the pressed element's own rect can't be
 * measured (e.g. zero-sized — an unmounted ref, or a test that doesn't stub
 * `getBoundingClientRect`). Matches the sidebar's own "md" persona avatar
 * (size-10 = 40px, see persona-avatar.tsx's SIZE_CLASSES) so the default
 * behaves sanely even when real layout isn't available.
 */
const DEFAULT_RADIUS_FALLBACK_PX = 20;

export function usePressSwipeGesture<T extends HTMLElement = HTMLElement>({
  onCommitUp,
  onCommitDown,
  slopPx = DEFAULT_SLOP_PX,
  radiusPx,
}: PressSwipeGestureOptions): PressSwipeGesture<T> {
  const [armed, setArmed] = useState(false);
  const [highlight, setHighlight] = useState<SwipeDirection | null>(null);
  const [progress, setProgress] = useState(0);
  const [anchorRect, setAnchorRect] = useState<DOMRect | null>(null);

  const elementRef = useRef<T | null>(null);
  const suppressClickRef = useRef(false);
  // Any in-flight gesture's teardown — a defensive unmount-time cleanup (the
  // sidebar's own live-refresh, R9-012, can re-render/unmount this list from
  // under an active swipe).
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
      const startY = event.clientY;
      const rect = elementRef.current?.getBoundingClientRect() ?? null;
      const measuredRadius = rect ? Math.min(rect.width, rect.height) / 2 : 0;
      const radius =
        radiusPx ??
        (measuredRadius > 0 ? measuredRadius : DEFAULT_RADIUS_FALLBACK_PX);
      const centerY = rect ? rect.top + rect.height / 2 : startY;
      let isSwiping = false;

      const detach = () => {
        document.removeEventListener("pointermove", handleMove);
        document.removeEventListener("pointerup", handleUp);
        document.removeEventListener("pointercancel", handleCancel);
        detachRef.current = null;
      };

      const finish = (commit: SwipeDirection | null) => {
        const wasSwiping = isSwiping;
        detach();
        isSwiping = false;
        setArmed(false);
        setHighlight(null);
        setProgress(0);
        setAnchorRect(null);
        if (wasSwiping) {
          // The gesture reached "swiping": its eventual click (the browser
          // fires one for this pointerdown/pointerup pair whenever the net
          // movement is small, same as any tap) must not ALSO navigate.
          suppressClickRef.current = true;
        }
        if (commit === "up") onCommitUp();
        else if (commit === "down") onCommitDown();
      };

      // Recompute the locked direction + continuous progress from the
      // pointer's CURRENT vertical distance to the circle's own center —
      // fully continuous, called on every move AND once more at release, so
      // "drag back inside cancels" and "release exactly at the rim" agree.
      function lockedDirection(clientY: number): SwipeDirection | null {
        const dyFromCenter = clientY - centerY;
        if (dyFromCenter <= -radius) return "up";
        if (dyFromCenter >= radius) return "down";
        return null;
      }

      function handleMove(moveEvent: PointerEvent) {
        if (moveEvent.pointerId !== pointerId) return;
        const dy = moveEvent.clientY - startY;
        if (!isSwiping) {
          // Pre-swipe: still deciding tap vs. swipe. Touch-action:none on
          // the bound element already keeps a touch drag from scrolling
          // anything underneath (R9-036 REOPEN's scroll trade-off), so
          // there's nothing else to guard here — just wait for slop.
          if (Math.abs(dy) <= slopPx) return;
          isSwiping = true;
          setAnchorRect(rect);
          setArmed(true);
          // Fall through: this same move already crossed into swiping mode,
          // so it also drives the first lock/progress read below.
        }
        moveEvent.preventDefault();
        const dyFromCenter = moveEvent.clientY - centerY;
        setProgress(Math.max(-1, Math.min(1, dyFromCenter / radius)));
        setHighlight(lockedDirection(moveEvent.clientY));
      }

      function handleUp(upEvent: PointerEvent) {
        if (upEvent.pointerId !== pointerId) return;
        if (!isSwiping) {
          // Released before slop was exceeded — a plain tap. Detach
          // quietly; the native click fires unmolested.
          detach();
          return;
        }
        finish(lockedDirection(upEvent.clientY));
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
    [onCommitUp, onCommitDown, slopPx, radiusPx],
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
    progress,
    anchorRect,
    elementRef,
    handlers: { onPointerDown, onClick },
  };
}
