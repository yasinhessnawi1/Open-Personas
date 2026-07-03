import { fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { beforeEach, describe, expect, it, vi } from "vitest";
import en from "@/i18n/messages/en.json";
import { CONNECTOR_CATALOGUE } from "@/lib/connectors/catalogue";
import type { ConnectorLinkArtifact } from "@/lib/connectors/connect-flow-machine";
import { DeepLinkStep } from "./deep-link-step";
import { ExpiryCountdown } from "./expiry-countdown";

/**
 * Spec C6 (T6) — the Telegram deep-link step: present the `t.me` link + copy affordance + the
 * contextual instruction (C6-D-5) + the server-authoritative expiry countdown (C6-D-8).
 */

const TELEGRAM = CONNECTOR_CATALOGUE.find((m) => m.key === "telegram");
if (!TELEGRAM) throw new Error("telegram missing from catalogue");

const BASE = Date.parse("2026-07-03T12:00:00Z");

function artifact(
  over: Partial<ConnectorLinkArtifact> = {},
): ConnectorLinkArtifact {
  return {
    deep_link: "https://t.me/openpersona_bot?start=TOKEN123",
    expires_at: new Date(BASE + 90_000).toISOString(), // 1:30 out
    code: null,
    destination: null,
    authorize_url: null,
    ...over,
  };
}

function renderWithIntl(ui: React.ReactNode) {
  return render(
    <NextIntlClientProvider locale="en" messages={en}>
      {ui}
    </NextIntlClientProvider>,
  );
}

describe("DeepLinkStep", () => {
  const writeText = vi.fn().mockResolvedValue(undefined);
  beforeEach(() => {
    writeText.mockClear();
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
    });
  });

  it("presents the deep link as an Open action + a contextual instruction", () => {
    renderWithIntl(<DeepLinkStep artifact={artifact()} meta={TELEGRAM} />);
    const open = screen.getByRole("link", { name: /Open Telegram/ });
    expect(open).toHaveAttribute(
      "href",
      "https://t.me/openpersona_bot?start=TOKEN123",
    );
    // Opens in a NEW tab — never navigates this page away, or the SPA (and the poll) would
    // unload and confirmation could never land (T6 carry-item).
    expect(open).toHaveAttribute("target", "_blank");
    expect(open).toHaveAttribute("rel", "noopener noreferrer");
    expect(screen.getByText(/press Start to finish/)).toBeInTheDocument();
  });

  it("copies the deep link to the clipboard and confirms", async () => {
    renderWithIntl(<DeepLinkStep artifact={artifact()} meta={TELEGRAM} />);
    fireEvent.click(screen.getByRole("button", { name: /Copy link/ }));
    expect(writeText).toHaveBeenCalledWith(
      "https://t.me/openpersona_bot?start=TOKEN123",
    );
    expect(await screen.findByText("Copied")).toBeInTheDocument();
  });
});

describe("ExpiryCountdown", () => {
  it("renders the remaining time to the server expires_at", () => {
    renderWithIntl(
      <ExpiryCountdown
        expiresAt={new Date(BASE + 90_000).toISOString()}
        now={() => BASE}
      />,
    );
    expect(screen.getByText("Expires in 1:30")).toBeInTheDocument();
  });

  it("shows Expired once the deadline has passed", () => {
    renderWithIntl(
      <ExpiryCountdown
        expiresAt={new Date(BASE).toISOString()}
        now={() => BASE + 5_000}
      />,
    );
    expect(screen.getByText("Expired")).toBeInTheDocument();
  });
});
