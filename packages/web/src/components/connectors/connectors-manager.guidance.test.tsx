import { render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { beforeEach, describe, expect, it, vi } from "vitest";
import en from "@/i18n/messages/en.json";

/**
 * Spec C6 (T10, bar 3) — first-connection guidance (C6-D-5): the none-connected state says
 * what connecting does + what to pick first, and invites ONLY platforms whose backend is
 * wired today (the wired-capability rule) — never Discord/Slack while their OAuth issue
 * routes are unmounted (C6-KL-2).
 */

const toastSpies = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
  warning: vi.fn(),
  info: vi.fn(),
}));

vi.mock("@/components/patterns/toast", () => ({ useToast: () => toastSpies }));
vi.mock("@/components/providers/confirm-provider", () => ({
  useConfirm: () => vi.fn(),
}));
vi.mock("@/lib/api/use-api", () => ({
  useApi: () => ({ POST: vi.fn(), DELETE: vi.fn() }),
}));
vi.mock("@/lib/connectors/use-connectors", () => ({
  useConnectors: () => ({
    connections: [],
    loading: false,
    error: null,
    refresh: vi.fn().mockResolvedValue([]),
  }),
}));

import { ConnectorsManager } from "./connectors-manager";

function renderWithIntl() {
  return render(
    <NextIntlClientProvider locale="en" messages={en}>
      <ConnectorsManager />
    </NextIntlClientProvider>,
  );
}

describe("ConnectorsManager — first-connection guidance", () => {
  beforeEach(() => {
    window.history.pushState(null, "", "/connectors"); // clean URL (no OAuth return)
  });

  it("shows guidance that invites only backend-ready platforms (not Discord/Slack)", () => {
    renderWithIntl();
    const guidance = document.querySelector(
      '[data-slot="first-connection-guidance"]',
    );
    expect(guidance).toBeInTheDocument();
    expect(guidance).toHaveTextContent("Get started");
    // Invites the wired platforms...
    expect(guidance).toHaveTextContent("Telegram");
    expect(guidance).toHaveTextContent("WhatsApp");
    expect(guidance).toHaveTextContent("SMS");
    expect(guidance).toHaveTextContent("Email");
    // ...but NOT the unmounted OAuth ones (C6-KL-2 wired-capability rule).
    expect(guidance?.textContent).not.toContain("Discord");
    expect(guidance?.textContent).not.toContain("Slack");
  });

  it("names what connecting does (C6-D-5 voice, not a manual)", () => {
    renderWithIntl();
    expect(
      screen.getByText(/your personas become reachable there/),
    ).toBeInTheDocument();
  });
});
