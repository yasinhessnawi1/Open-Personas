/**
 * R9-036 — `<PressSwipePreview>` render/highlight/positioning tests.
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

describe("PressSwipePreview", () => {
  it("renders nothing when not armed", () => {
    const { container } = render(
      <PressSwipePreview
        armed={false}
        highlight={null}
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
        anchorRect={null}
        collapsed={false}
        up={UP}
        down={DOWN}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("renders both chips (armed, no highlight) via a portal, hidden from AT", () => {
    render(
      <PressSwipePreview
        armed={true}
        highlight={null}
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
    const overlay = document.querySelector('[data-slot="press-swipe-preview"]');
    expect(overlay).toHaveAttribute("aria-hidden", "true");
    // Portaled straight onto <body>, not nested under the render container.
    expect(overlay?.parentElement).toBe(document.body);
  });

  it("marks the 'up' chip highlighted and the 'down' chip neutral", () => {
    render(
      <PressSwipePreview
        armed={true}
        highlight="up"
        anchorRect={RECT}
        collapsed={false}
        up={UP}
        down={DOWN}
      />,
    );
    const chips = document.querySelectorAll('[data-slot="press-swipe-chip"]');
    expect(chips).toHaveLength(2);
    const [upChip, downChip] = Array.from(chips);
    expect(upChip).toHaveAttribute("data-highlighted", "true");
    expect(downChip).toHaveAttribute("data-highlighted", "false");
    expect(upChip.className).toContain("bg-primary");
    expect(downChip.className).not.toContain("bg-primary");
  });

  it("marks the 'down' chip highlighted when highlight='down'", () => {
    render(
      <PressSwipePreview
        armed={true}
        highlight="down"
        anchorRect={RECT}
        collapsed={false}
        up={UP}
        down={DOWN}
      />,
    );
    const chips = document.querySelectorAll('[data-slot="press-swipe-chip"]');
    const [upChip, downChip] = Array.from(chips);
    expect(upChip).toHaveAttribute("data-highlighted", "false");
    expect(downChip).toHaveAttribute("data-highlighted", "true");
  });

  it("neither chip is highlighted when highlight is null (the neutral live-preview state)", () => {
    render(
      <PressSwipePreview
        armed={true}
        highlight={null}
        anchorRect={RECT}
        collapsed={false}
        up={UP}
        down={DOWN}
      />,
    );
    for (const chip of document.querySelectorAll(
      '[data-slot="press-swipe-chip"]',
    )) {
      expect(chip).toHaveAttribute("data-highlighted", "false");
    }
  });

  it("expanded: positions the up chip above and the down chip below the anchor", () => {
    render(
      <PressSwipePreview
        armed={true}
        highlight={null}
        anchorRect={RECT}
        collapsed={false}
        up={UP}
        down={DOWN}
      />,
    );
    const [upChip, downChip] = Array.from(
      document.querySelectorAll<HTMLElement>('[data-slot="press-swipe-chip"]'),
    );
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
        anchorRect={RECT}
        collapsed={true}
        up={UP}
        down={DOWN}
      />,
    );
    const [upChip, downChip] = Array.from(
      document.querySelectorAll<HTMLElement>('[data-slot="press-swipe-chip"]'),
    );
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
        anchorRect={RECT}
        collapsed={false}
        up={UP}
        down={DOWN}
      />,
    );
    for (const chip of document.querySelectorAll(
      '[data-slot="press-swipe-chip"]',
    )) {
      expect(chip.className).toContain("motion-reduce:animate-none");
      expect(chip.className).toContain("motion-reduce:transition-none");
    }
  });

  it("animates only transform/opacity-flavoured utilities (no colour/background transition)", () => {
    render(
      <PressSwipePreview
        armed={true}
        highlight={null}
        anchorRect={RECT}
        collapsed={false}
        up={UP}
        down={DOWN}
      />,
    );
    const chip = document.querySelector('[data-slot="press-swipe-chip"]');
    expect(chip?.className).toContain("fade-in-0"); // opacity entrance
    expect(chip?.className).toContain("transition-transform"); // transform only
    expect(chip?.className).not.toMatch(
      /transition-colors|transition-\[.*color/,
    );
  });
});
