/**
 * Spec 21 T11 — <AutonomyConsentSection> tests (D-21-1/2).
 *
 * Autonomy selector (3 levels) + consent toggle with revocation warning. The
 * toggle off-path revokes to "ask" (null), not decline; the warning shows only
 * when consent is granted.
 */
import { fireEvent, render as rtlRender, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import messages from "@/i18n/messages/en.json";
import { AutonomyConsentSection } from "./autonomy-consent-section";

/** The section's copy lives in `personaPage.autonomy.*` now. */
const render = (ui: ReactNode) =>
  rtlRender(
    <NextIntlClientProvider locale="en" messages={messages}>
      {ui}
    </NextIntlClientProvider>,
  );

const base = {
  autonomy: "cautious" as const,
  onAutonomyChange: vi.fn(),
  consent: null,
  onConsentChange: vi.fn(),
};

describe("AutonomyConsentSection — autonomy selector", () => {
  it("renders all three levels", () => {
    render(<AutonomyConsentSection {...base} />);
    expect(screen.getByText("Cautious")).toBeInTheDocument();
    expect(screen.getByText("Balanced")).toBeInTheDocument();
    expect(screen.getByText("Decisive")).toBeInTheDocument();
  });

  it("marks the current level as pressed", () => {
    render(<AutonomyConsentSection {...base} autonomy="balanced" />);
    const balanced = screen.getByText("Balanced").closest("button");
    expect(balanced).toHaveAttribute("aria-pressed", "true");
  });

  it("calls onAutonomyChange when a level is clicked", () => {
    const onAutonomyChange = vi.fn();
    render(
      <AutonomyConsentSection {...base} onAutonomyChange={onAutonomyChange} />,
    );
    fireEvent.click(screen.getByText("Decisive"));
    expect(onAutonomyChange).toHaveBeenCalledWith("decisive");
  });
});

describe("AutonomyConsentSection — consent toggle (D-21-2)", () => {
  it("shows Off and no warning when not granted", () => {
    render(<AutonomyConsentSection {...base} consent={null} />);
    const sw = screen.getByRole("switch");
    expect(sw).toHaveAttribute("aria-checked", "false");
    expect(screen.queryByText(/Turning this off/)).not.toBeInTheDocument();
  });

  it("shows On and a revocation warning when granted", () => {
    render(<AutonomyConsentSection {...base} consent={true} />);
    const sw = screen.getByRole("switch");
    expect(sw).toHaveAttribute("aria-checked", "true");
    expect(screen.getByText(/Turning this off/)).toBeInTheDocument();
  });

  it("granting calls onConsentChange(true)", () => {
    const onConsentChange = vi.fn();
    render(
      <AutonomyConsentSection
        {...base}
        consent={null}
        onConsentChange={onConsentChange}
      />,
    );
    fireEvent.click(screen.getByRole("switch"));
    expect(onConsentChange).toHaveBeenCalledWith(true);
  });

  it("toggling off revokes to ask (null), not decline", () => {
    const onConsentChange = vi.fn();
    render(
      <AutonomyConsentSection
        {...base}
        consent={true}
        onConsentChange={onConsentChange}
      />,
    );
    fireEvent.click(screen.getByRole("switch"));
    expect(onConsentChange).toHaveBeenCalledWith(null);
  });
});
