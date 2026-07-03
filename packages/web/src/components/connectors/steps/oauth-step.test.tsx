import { render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it } from "vitest";
import en from "@/i18n/messages/en.json";
import { CONNECTOR_CATALOGUE } from "@/lib/connectors/catalogue";
import type { ConnectorLinkArtifact } from "@/lib/connectors/connect-flow-machine";
import { OAuthStep } from "./oauth-step";

/**
 * Spec C6 (T7) — the OAuth step is an explicit FULL-PAGE navigation to authorize_url (no
 * popup, no iframe, no new tab), with honest "you're leaving" copy.
 */

const DISCORD = CONNECTOR_CATALOGUE.find((m) => m.key === "discord");
if (!DISCORD) throw new Error("discord missing from catalogue");

function artifact(): ConnectorLinkArtifact {
  return {
    authorize_url:
      "https://discord.com/oauth2/authorize?client_id=c&state=TOKEN",
    expires_at: "2099-01-01T00:00:00+00:00",
    code: null,
    destination: null,
    deep_link: null,
  };
}

function renderWithIntl(ui: React.ReactNode) {
  return render(
    <NextIntlClientProvider locale="en" messages={en}>
      {ui}
    </NextIntlClientProvider>,
  );
}

describe("OAuthStep", () => {
  it("navigates full-page to authorize_url (same tab, no popup/new-tab)", () => {
    renderWithIntl(<OAuthStep artifact={artifact()} meta={DISCORD} />);
    const link = screen.getByRole("link", { name: /Continue to Discord/ });
    expect(link).toHaveAttribute(
      "href",
      "https://discord.com/oauth2/authorize?client_id=c&state=TOKEN",
    );
    // NOT a new tab — OAuth is a same-tab redirect out to the provider and back (C6-D-2).
    expect(link).not.toHaveAttribute("target", "_blank");
  });

  it("tells the user honestly that they are leaving the app", () => {
    renderWithIntl(<OAuthStep artifact={artifact()} meta={DISCORD} />);
    expect(screen.getByText(/go to Discord to authorize/)).toBeInTheDocument();
  });
});
