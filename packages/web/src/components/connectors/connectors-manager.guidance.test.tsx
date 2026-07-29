import { render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { beforeEach, describe, expect, it, vi } from "vitest";
import en from "@/i18n/messages/en.json";
import { CONNECTOR_CATALOGUE } from "@/lib/connectors/catalogue";

/**
 * Spec C6 (T10, bar 3) — first-connection guidance (C6-D-5): the none-connected state says
 * what connecting does + what to pick first, and invites ONLY platforms whose backend is
 * wired today (the wired-capability rule) — never one whose Connect cannot succeed.
 *
 * Which platforms those are is DELIBERATELY not hardcoded here: readiness changes with
 * reality (R9-061 mounted Discord/Slack's routes, making them ready; WhatsApp/SMS went
 * unready pending a paid Twilio subscription). The test derives both sets from
 * ``CONNECTOR_CATALOGUE`` so it keeps proving the rule instead of a snapshot of one day's
 * configuration.
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
    window.history.pushState(null, "", "/settings/connectors"); // clean URL (no OAuth return)
  });

  it("invites exactly the backend-ready platforms, and no unready one", () => {
    renderWithIntl();
    const guidance = document.querySelector(
      '[data-slot="first-connection-guidance"]',
    );
    expect(guidance).toBeInTheDocument();
    expect(guidance).toHaveTextContent("Get started");

    // Derive the expectation from the catalogue itself rather than hardcoding today's
    // platform split: readiness legitimately changes (Discord/Slack became ready once
    // R9-061 mounted their routes; WhatsApp/SMS went unready pending a Twilio
    // subscription). The INVARIANT under test is the wired-capability rule (T10) —
    // guidance names every ready platform and never names an unready one — which must
    // hold at any configuration, so this test no longer breaks on a readiness flip.
    const ready = CONNECTOR_CATALOGUE.filter((m) => m.backendReady);
    const unready = CONNECTOR_CATALOGUE.filter((m) => !m.backendReady);
    expect(ready.length).toBeGreaterThan(0); // guard: a vacuous pass proves nothing
    for (const meta of ready) {
      expect(guidance).toHaveTextContent(en.connectors.platform[meta.key]);
    }
    for (const meta of unready) {
      expect(guidance?.textContent).not.toContain(
        en.connectors.platform[meta.key],
      );
    }
  });

  it("names what connecting does (C6-D-5 voice, not a manual)", () => {
    renderWithIntl();
    expect(
      screen.getByText(/your personas become reachable there/),
    ).toBeInTheDocument();
  });
});
