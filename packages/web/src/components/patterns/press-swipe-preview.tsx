"use client";

/**
 * R9-036 REOPEN — the live-preview chips for `usePressSwipeGesture`
 * (lib/hooks/use-press-swipe-gesture.ts).
 *
 * A generic, portal-rendered pair of "up" / "down" chip cards anchored
 * beside whatever element is currently being swiped. Presentation-only: it
 * knows nothing about call/chat — the caller supplies each direction's icon
 * + already-translated label, so any future press-swipe surface can reuse
 * it for its own action pair. The directional arrow (up/down) is NOT
 * caller-supplied — it's inherent to which chip this is, so it's rendered
 * here.
 *
 * Positioning: `position: fixed`, computed from the anchor's
 * `getBoundingClientRect()` (captured by the gesture hook the instant
 * swiping starts) and portaled to `document.body` — this is deliberate, not
 * just convenience: a `fixed`-positioned in-place child would still get
 * clipped by the sidebar's `<ScrollArea>` (`overflow` ancestor), and in the
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
 * Surface (R9-036 REOPEN point 5 — "more designed and styled", not the
 * original's utilitarian pill): an elevated CARD, mirroring how
 * command-palette.tsx's popup and tooltip.tsx's bubble already read as
 * "floating house surfaces" — `border-border` + `bg-popover` +
 * `shadow-[var(--elevation-2)]` at rest. Locked state swaps to the primary
 * surface (`bg-primary` / `border-primary` / `text-primary-foreground`),
 * lifts to `--elevation-3` (a taller shadow reads as "closer, about to
 * commit"), and gains a soft colour-ring GLOW (`ring-primary/40` — the same
 * coloured-ring convention error-state.tsx already uses for its own
 * credits-exhausted affordance) instead of inventing a bespoke glow shadow.
 *
 * Live connection to the gesture (R9-036 REOPEN: "a visible connection to
 * the gesture"): each chip's scale and opacity are DERIVED, continuously,
 * from the gesture's signed `progress` (-1 at the "up" rim, 0 centered, +1
 * at the "down" rim) — not just a binary locked/unlocked snap. As the
 * pointer nears a chip's own threshold it brightens (opacity ramps toward
 * 1) and grows (scale ramps toward the ~1.08 locked size); the opposite
 * chip visibly recedes (dims) once a direction actually locks, so the pair
 * always reads as "here are your two options, here's which one is about to
 * fire." Only `transform` (the scale) and `opacity` are ever animated
 * (R9-036 REOPEN's explicit constraint) — background/border/ring/shadow
 * tier all swap on lock as a plain className change with NO transition
 * property covering them, so they snap instantly rather than crossfading.
 * `motion-reduce:animate-none` + `motion-reduce:transition-none` make both
 * the entrance and the continuous scale/opacity instant under reduced
 * motion — since scale/opacity are recomputed on every pointer move
 * regardless, disabling the CSS transition alone is sufficient to make
 * every state change a discrete jump (redundant with the F1 T15 global
 * `prefers-reduced-motion` CSS override in globals.css, which already zeros
 * animation/transition durations app-wide — kept explicit here too,
 * matching sidebar.tsx's own belt-and-suspenders style, and so the contract
 * is unit-testable).
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

import { ArrowDown, ArrowUp } from "lucide-react";
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
  /** Render nothing until the gesture is swiping (past the tap-slop). */
  armed: boolean;
  /** Which direction is currently locked (past the circle's rim), or neutral. */
  highlight: SwipeDirection | null;
  /** Signed continuous progress toward each lock, [-1, 1] — see the gesture hook. */
  progress: number;
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
/** Locked-chip scale — a visible but restrained "about to commit" cue (owner's ~1.06-1.1 range). */
const LOCKED_SCALE = 1.08;
/** Resting opacity once swiping but before the pointer meaningfully approaches THIS chip's own direction. */
const REST_OPACITY = 0.85;
/** Opacity when the OPPOSITE direction is locked — this chip visibly recedes. */
const DIMMED_OPACITY = 0.4;

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

/** This chip's own 0..1 proximity to ITS lock threshold, derived from the shared signed progress. */
function proximity(progress: number, direction: SwipeDirection): number {
  const signed = direction === "up" ? -progress : progress;
  return Math.max(0, Math.min(1, signed));
}

function Chip({
  action,
  direction,
  locked,
  oppositeLocked,
  progress,
  style,
}: {
  action: PressSwipeAction;
  direction: SwipeDirection;
  locked: boolean;
  oppositeLocked: boolean;
  progress: number;
  style: CSSProperties;
}) {
  const near = proximity(progress, direction);
  const scale = 1 + near * (LOCKED_SCALE - 1);
  const opacity = oppositeLocked
    ? DIMMED_OPACITY
    : REST_OPACITY + near * (1 - REST_OPACITY);
  const ArrowIcon = direction === "up" ? ArrowUp : ArrowDown;

  return (
    <div
      data-slot="press-swipe-chip"
      data-direction={direction}
      data-highlighted={locked}
      style={{
        ...style,
        transform: `${style.transform} scale(${scale})`,
        opacity,
      }}
      className={cn(
        "z-50 flex items-center gap-1.5 whitespace-nowrap rounded-full border px-3.5 py-2 type-caption normal-case tracking-normal shadow-[var(--elevation-2)]",
        "animate-in fade-in-0 duration-[var(--motion-duration-fast)] ease-[var(--motion-ease-standard)] motion-reduce:animate-none",
        "transition-[transform,opacity] motion-reduce:transition-none",
        locked
          ? "border-primary/50 bg-primary text-primary-foreground shadow-[var(--elevation-3)] ring-2 ring-primary/40"
          : "border-border bg-popover text-popover-foreground",
      )}
    >
      {action.icon}
      {action.label}
      <ArrowIcon className="size-3 opacity-70" aria-hidden="true" />
    </div>
  );
}

/**
 * Renders `null` on the server and on every render until a real pointer
 * gesture starts swiping (`armed` starts `false` and can only flip via a
 * live `pointerdown` + past-slop move — never during SSR or the first
 * client paint), so there's no hydration mismatch to guard against and no
 * mount-gating effect needed: the portal only ever appears in response to
 * an actual gesture.
 */
export function PressSwipePreview({
  armed,
  highlight,
  progress,
  anchorRect,
  collapsed,
  up,
  down,
}: PressSwipePreviewProps) {
  if (!armed || anchorRect === null) return null;

  const pos = positions(anchorRect, collapsed);

  return createPortal(
    <div aria-hidden="true" data-slot="press-swipe-preview">
      <Chip
        action={up}
        direction="up"
        locked={highlight === "up"}
        oppositeLocked={highlight === "down"}
        progress={progress}
        style={pos.up}
      />
      <Chip
        action={down}
        direction="down"
        locked={highlight === "down"}
        oppositeLocked={highlight === "up"}
        progress={progress}
        style={pos.down}
      />
    </div>,
    document.body,
  );
}
