/**
 * R9-036 REOPEN — `<PressSwipePreview>` render/highlight/positioning/styling
 * tests.
 *
 * `createPortal(..., document.body)` output lands as a sibling of
 * Testing Library's own render container (RTL appends its container to
 * `document.body`), so the default `screen` bindings (which query from
 * `document.body`) find the portaled chips without any extra setup.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { PressSwipePreview } from "./press-swipe-preview";

const RECT: DOMRect = {
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
};

const UP = { icon: <span data-testid="up-icon" />, label: "Call Astrid" };
const DOWN = { icon: <span data-testid="down-icon" />, label: "New chat" };

/** The two chip elements, in "up", "down" order. */
function chips(): [HTMLElement, HTMLElement] {
  const [upChip, downChip] = Array.from(
    document.querySelectorAll<HTMLElement>('[data-slot="press-swipe-chip"]'),
  );
  return [upChip, downChip];
}

/** Parses the `scale(N)` factor out of a chip's inline `transform`. */
function scaleOf(el: HTMLElement): number {
  const match = /scale\(([\d.]+)\)/.exec(el.style.transform);
  if (!match) throw new Error(`no scale() in transform: ${el.style.transform}`);
  return Number.parseFloat(match[1]);
}

describe("PressSwipePreview", () => {
  it("renders nothing when not armed", () => {
    const { container } = render(
      <PressSwipePreview
        armed={false}
        highlight={null}
        progress={0}
        anchorRect={RECT}
        collapsed={false}
        up={UP}
        down={DOWN}
      />,
    );
    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByText("Call Astrid")).not.toBeInTheDocument();
  });

  it("renders nothing when armed but the anchor rect isn't captured yet", () => {
    const { container } = render(
      <PressSwipePreview
        armed={true}
        highlight={null}
        progress={0}
        anchorRect={null}
        collapsed={false}
        up={UP}
        down={DOWN}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("renders icon + label + directional arrow for both chips, via a portal, hidden from AT", () => {
    render(
      <PressSwipePreview
        armed={true}
        highlight={null}
        progress={0}
        anchorRect={RECT}
        collapsed={false}
        up={UP}
        down={DOWN}
      />,
    );
    expect(screen.getByText("Call Astrid")).toBeInTheDocument();
    expect(screen.getByText("New chat")).toBeInTheDocument();
    expect(screen.getByTestId("up-icon")).toBeInTheDocument();
    expect(screen.getByTestId("down-icon")).toBeInTheDocument();
    // The caller-supplied icons are plain <span>s (see UP/DOWN above) — any
    // <svg> found inside a chip must be the directional arrow this
    // component renders itself.
    const [upChip, downChip] = chips();
    expect(upChip.querySelector("svg")).toBeInTheDocument();
    expect(downChip.querySelector("svg")).toBeInTheDocument();
    const overlay = document.querySelector('[data-slot="press-swipe-preview"]');
    expect(overlay).toHaveAttribute("aria-hidden", "true");
    // Portaled straight onto <body>, not nested under the render container.
    expect(overlay?.parentElement).toBe(document.body);
  });

  it("marks the 'up' chip locked and the 'down' chip neutral", () => {
    render(
      <PressSwipePreview
        armed={true}
        highlight="up"
        progress={-1}
        anchorRect={RECT}
        collapsed={false}
        up={UP}
        down={DOWN}
      />,
    );
    const [upChip, downChip] = chips();
    expect(upChip).toHaveAttribute("data-highlighted", "true");
    expect(downChip).toHaveAttribute("data-highlighted", "false");
    expect(upChip.className).toContain("bg-primary");
    expect(downChip.className).not.toContain("bg-primary");
  });

  it("marks the 'down' chip locked when highlight='down'", () => {
    render(
      <PressSwipePreview
        armed={true}
        highlight="down"
        progress={1}
        anchorRect={RECT}
        collapsed={false}
        up={UP}
        down={DOWN}
      />,
    );
    const [upChip, downChip] = chips();
    expect(upChip).toHaveAttribute("data-highlighted", "false");
    expect(downChip).toHaveAttribute("data-highlighted", "true");
  });

  it("neither chip is locked when highlight is null (the neutral live-preview state)", () => {
    render(
      <PressSwipePreview
        armed={true}
        highlight={null}
        progress={0}
        anchorRect={RECT}
        collapsed={false}
        up={UP}
        down={DOWN}
      />,
    );
    for (const chip of chips()) {
      expect(chip).toHaveAttribute("data-highlighted", "false");
    }
  });

  it("a locked chip lifts to the taller elevation tier and gains a colour-ring glow", () => {
    render(
      <PressSwipePreview
        armed={true}
        highlight="down"
        progress={1}
        anchorRect={RECT}
        collapsed={false}
        up={UP}
        down={DOWN}
      />,
    );
    const [, downChip] = chips();
    expect(downChip.className).toContain("shadow-[var(--elevation-3)]");
    expect(downChip.className).toContain("ring-primary/40");
    expect(downChip.className).not.toContain("shadow-[var(--elevation-2)]");
  });

  it("a resting (unlocked) chip uses the base elevation tier with a plain border surface", () => {
    render(
      <PressSwipePreview
        armed={true}
        highlight={null}
        progress={0}
        anchorRect={RECT}
        collapsed={false}
        up={UP}
        down={DOWN}
      />,
    );
    const [upChip] = chips();
    expect(upChip.className).toContain("shadow-[var(--elevation-2)]");
    expect(upChip.className).toContain("border-border");
    expect(upChip.className).toContain("bg-popover");
  });

  it("brightens and grows the approaching chip as progress nears its own lock, while the other chip stays at rest", () => {
    render(
      <PressSwipePreview
        armed={true}
        highlight={null}
        progress={-0.5} // halfway toward the "up" lock
        anchorRect={RECT}
        collapsed={false}
        up={UP}
        down={DOWN}
      />,
    );
    const [upChip, downChip] = chips();
    const upOpacity = Number.parseFloat(upChip.style.opacity);
    const downOpacity = Number.parseFloat(downChip.style.opacity);
    expect(upOpacity).toBeGreaterThan(downOpacity);
    expect(scaleOf(upChip)).toBeGreaterThan(scaleOf(downChip));
    // The chip that isn't being approached stays at its resting scale.
    expect(scaleOf(downChip)).toBeCloseTo(1);
  });

  it("locked chip reaches full opacity and a scale within the owner's ~1.06-1.1 range; the opposite chip dims", () => {
    render(
      <PressSwipePreview
        armed={true}
        highlight="up"
        progress={-1}
        anchorRect={RECT}
        collapsed={false}
        up={UP}
        down={DOWN}
      />,
    );
    const [upChip, downChip] = chips();
    expect(Number.parseFloat(upChip.style.opacity)).toBeCloseTo(1);
    const lockedScale = scaleOf(upChip);
    expect(lockedScale).toBeGreaterThanOrEqual(1.06);
    expect(lockedScale).toBeLessThanOrEqual(1.1);
    // The opposite (down) chip is NOT locked — it visibly recedes rather
    // than sitting at the neutral resting look.
    expect(Number.parseFloat(downChip.style.opacity)).toBeLessThan(0.5);
  });

  it("expanded: positions the up chip above and the down chip below the anchor", () => {
    render(
      <PressSwipePreview
        armed={true}
        highlight={null}
        progress={0}
        anchorRect={RECT}
        collapsed={false}
        up={UP}
        down={DOWN}
      />,
    );
    const [upChip, downChip] = chips();
    const upTop = Number.parseFloat(upChip.style.top);
    const downTop = Number.parseFloat(downChip.style.top);
    expect(upTop).toBeLessThan(RECT.top);
    expect(downTop).toBeGreaterThan(RECT.bottom);
    // Horizontally centered on the anchor in expanded mode.
    expect(Number.parseFloat(upChip.style.left)).toBeCloseTo(
      RECT.left + RECT.width / 2,
    );
  });

  it("collapsed: flyouts both chips to the right of the (narrow) rail anchor", () => {
    render(
      <PressSwipePreview
        armed={true}
        highlight={null}
        progress={0}
        anchorRect={RECT}
        collapsed={true}
        up={UP}
        down={DOWN}
      />,
    );
    const [upChip, downChip] = chips();
    // Both chips clear the anchor's right edge — never clipped against a
    // narrow 64px rail.
    expect(Number.parseFloat(upChip.style.left)).toBeGreaterThanOrEqual(
      RECT.right,
    );
    expect(Number.parseFloat(downChip.style.left)).toBeGreaterThanOrEqual(
      RECT.right,
    );
    // Stacked call-above-chat around the anchor's vertical center.
    const upTop = Number.parseFloat(upChip.style.top);
    const downTop = Number.parseFloat(downChip.style.top);
    expect(upTop).toBeLessThan(downTop);
  });

  it("reduced-motion branch: both animation and transition are neutralised for motion-reduce", () => {
    render(
      <PressSwipePreview
        armed={true}
        highlight="up"
        progress={-1}
        anchorRect={RECT}
        collapsed={false}
        up={UP}
        down={DOWN}
      />,
    );
    for (const chip of chips()) {
      expect(chip.className).toContain("motion-reduce:animate-none");
      expect(chip.className).toContain("motion-reduce:transition-none");
    }
  });

  it("animates only transform/opacity-flavoured utilities (no colour/background transition)", () => {
    render(
      <PressSwipePreview
        armed={true}
        highlight={null}
        progress={0}
        anchorRect={RECT}
        collapsed={false}
        up={UP}
        down={DOWN}
      />,
    );
    const [chip] = chips();
    expect(chip.className).toContain("fade-in-0"); // opacity entrance
    expect(chip.className).toContain("transition-[transform,opacity]");
    expect(chip.className).not.toMatch(
      /transition-colors|transition-\[.*color/,
    );
  });
});
