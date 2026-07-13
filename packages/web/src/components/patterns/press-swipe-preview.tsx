"use client";

/**
 * R9-036 — the live-preview chips for `usePressSwipeGesture`
 * (lib/hooks/use-press-swipe-gesture.ts).
 *
 * A generic, portal-rendered pair of "up" / "down" chips anchored beside
 * whatever element is currently armed. Presentation-only: it knows nothing
 * about call/chat — the caller supplies each direction's icon + already-
 * translated label, so any future press-swipe surface can reuse it for its
 * own action pair.
 *
 * Positioning: `position: fixed`, computed from the anchor's
 * `getBoundingClientRect()` (captured by the gesture hook the instant it
 * arms) and portaled to `document.body` — this is deliberate, not just
 * convenience: a `fixed`-positioned in-place child would still get clipped
 * by the sidebar's `<ScrollArea>` (`overflow` ancestor), and in the
 * collapsed 64px icon rail there's no room for the chips at all without
 * escaping it (mirrors why `<TooltipContent>` / `<DropdownMenuContent>`
 * portal too — components/ui/tooltip.tsx, dropdown-menu.tsx).
 *
 *   - Expanded: chips sit directly above / below the anchor, centered.
 *   - Collapsed (narrow rail): both chips flyout to the RIGHT of the anchor,
 *     stacked call-above-chat — the same "escape the rail" direction
 *     `<TooltipContent side="right">` already uses for every collapsed
 *     sidebar row (see sidebar-sections.tsx), so the chips never clip
 *     against the rail's edge.
 *
 * Motion: transform + opacity only (R9-036's explicit constraint) — an
 * opacity-only entrance (`animate-in fade-in-0`, a tw-animate-css keyframe,
 * so it plays correctly from the very first mounted frame — no pre-mount
 * "closed" frame needed) plus a `transition-transform` scale bump while
 * highlighted. Background/text colour swap on highlight is a plain
 * className change with NO transition property listed for it, so it snaps
 * instantly rather than crossfading — keeping the only animated properties
 * to transform/opacity as required. `motion-reduce:animate-none` +
 * `motion-reduce:transition-none` make both instant under reduced motion
 * (redundant with the F1 T15 global `prefers-reduced-motion` CSS override in
 * globals.css, which already zeros animation/transition durations
 * app-wide — kept explicit here too, matching sidebar.tsx's own
 * belt-and-suspenders style, and so the contract is unit-testable).
 *
 * Exit: immediate unmount, no exit animation. A deliberate asymmetry: the
 * gesture's release is itself the decisive, instantaneous completion of the
 * interaction (a commit navigates away; a cancel just means "stop
 * previewing") — a lingering fade-out of an already-irrelevant chip would be
 * noise, not enhancement, and avoids needing a presence/exit-animation
 * lifecycle (React's conditional render already unmounts synchronously).
 *
 * Accessibility: `aria-hidden` on the whole overlay. This is a pointer/touch
 * *enhancement* with no keyboard equivalent (R9-036: "the gesture is
 * enhancement-only — click paths remain the accessible path") — the chips
 * are a transient visual echo of a drag already in progress, not new
 * information a keyboard/AT user could otherwise reach or act on.
 */

import type { CSSProperties, ReactNode } from "react";
import { createPortal } from "react-dom";
import type { SwipeDirection } from "@/lib/hooks/use-press-swipe-gesture";
import { cn } from "@/lib/utils";

/** One chip's content — the caller owns icon + copy (already translated). */
export interface PressSwipeAction {
  icon: ReactNode;
  label: string;
}

export interface PressSwipePreviewProps {
  /** Render nothing until the gesture has armed. */
  armed: boolean;
  /** Which direction is currently past the commit threshold, or neutral. */
  highlight: SwipeDirection | null;
  /** The anchor's viewport rect (from the gesture hook); `null` → render nothing. */
  anchorRect: DOMRect | null;
  /** Narrow-rail flyout-beside-anchor layout vs. above/below the anchor. */
  collapsed: boolean;
  /** The "up" (drag-up-to-commit) chip's content. */
  up: PressSwipeAction;
  /** The "down" (drag-down-to-commit) chip's content. */
  down: PressSwipeAction;
}

/** Gap (px) between the anchor and a chip — matches Tailwind's `gap-2` (0.5rem),
 * this codebase's standard small related-item spacing. */
const ANCHOR_GAP = 8;
/** Vertical gap (px) between the two stacked chips in collapsed flyout mode —
 * same token as ANCHOR_GAP, kept as a separate constant since the two gaps are
 * conceptually different (anchor-to-chip vs chip-to-chip) even though equal today. */
const STACK_GAP = 8;
/** Highlighted-chip scale bump — a visible but restrained "about to commit" cue. */
const HIGHLIGHT_SCALE = 1.1;

function chipStyle(
  transform: string,
  top: number,
  left: number,
): CSSProperties {
  return { position: "fixed", top, left, transform };
}

function positions(
  rect: DOMRect,
  collapsed: boolean,
): { up: CSSProperties; down: CSSProperties } {
  if (collapsed) {
    // Flyout beside the (narrow) rail: both chips to the right of the
    // avatar, stacked call-above-chat around its vertical center.
    const centerY = rect.top + rect.height / 2;
    const left = rect.right + ANCHOR_GAP;
    return {
      up: chipStyle("translate(0, -100%)", centerY - STACK_GAP / 2, left),
      down: chipStyle("translate(0, 0)", centerY + STACK_GAP / 2, left),
    };
  }
  const centerX = rect.left + rect.width / 2;
  return {
    up: chipStyle("translate(-50%, -100%)", rect.top - ANCHOR_GAP, centerX),
    down: chipStyle("translate(-50%, 0)", rect.bottom + ANCHOR_GAP, centerX),
  };
}

function Chip({
  action,
  highlighted,
  style,
}: {
  action: PressSwipeAction;
  highlighted: boolean;
  style: CSSProperties;
}) {
  const scale = highlighted ? HIGHLIGHT_SCALE : 1;
  return (
    <div
      data-slot="press-swipe-chip"
      data-highlighted={highlighted}
      style={{
        ...style,
        transform: `${style.transform} scale(${scale})`,
      }}
      className={cn(
        "z-50 flex items-center gap-1.5 whitespace-nowrap rounded-full px-3 py-1.5 type-caption normal-case tracking-normal shadow-[var(--elevation-2)] ring-1 ring-foreground/10",
        "animate-in fade-in-0 duration-[var(--motion-duration-fast)] ease-[var(--motion-ease-standard)] motion-reduce:animate-none",
        "transition-transform motion-reduce:transition-none",
        highlighted
          ? "bg-primary text-primary-foreground"
          : "bg-popover text-popover-foreground",
      )}
    >
      {action.icon}
      {action.label}
    </div>
  );
}

/**
 * Renders `null` on the server and on every render until a real pointer
 * gesture arms (`armed` starts `false` and can only flip via a live
 * `pointerdown` + hold — never during SSR or the first client paint), so
 * there's no hydration mismatch to guard against and no mount-gating effect
 * needed: the portal only ever appears in response to an actual gesture.
 */
export function PressSwipePreview({
  armed,
  highlight,
  anchorRect,
  collapsed,
  up,
  down,
}: PressSwipePreviewProps) {
  if (!armed || anchorRect === null) return null;

  const pos = positions(anchorRect, collapsed);

  return createPortal(
    <div aria-hidden="true" data-slot="press-swipe-preview">
      <Chip action={up} highlighted={highlight === "up"} style={pos.up} />
      <Chip action={down} highlighted={highlight === "down"} style={pos.down} />
    </div>,
    document.body,
  );
}
