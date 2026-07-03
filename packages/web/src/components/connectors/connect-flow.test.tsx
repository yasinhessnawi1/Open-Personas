import { render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it, vi } from "vitest";
import en from "@/i18n/messages/en.json";
import { CONNECTOR_CATALOGUE } from "@/lib/connectors/catalogue";

/**
 * Spec C6 (T7, bar 5) — when the connector-service issue route isn't mounted (C6-KL-2,
 * Discord/Slack today), the front-door proxy fails soft with a 503, so the flow lands in the
 * honest `failed` voice with a retry — NEVER a broken Continue button (the OAuth step only
 * renders with a real authorize_url). This proves the deferred-mount degrades gracefully.
 */

// The proxy POST returns a 503 (as the fail-soft front-door does when the upstream is absent).
vi.mock("@/lib/api/use-api", () => ({
  useApi: () => ({
    POST: async () => ({
      error: { error: "connector_unavailable" },
      response: { status: 503, headers: new Headers() },
    }),
  }),
}));

import { ConnectFlow } from "./connect-flow";

const DISCORD = CONNECTOR_CATALOGUE.find((m) => m.key === "discord");
if (!DISCORD) throw new Error("discord missing from catalogue");

afterEach(() => vi.restoreAllMocks());

describe("ConnectFlow — deferred-mount / unavailable", () => {
  it("renders the honest unavailable voice + retry, not a broken button, on a 503", async () => {
    render(
      <NextIntlClientProvider locale="en" messages={en}>
        <ConnectFlow
          meta={DISCORD}
          connected={false}
          refresh={async () => []}
          onClose={() => {}}
        />
      </NextIntlClientProvider>,
    );

    // Auto-initiate → 503 → failed. The honest voice, not a Continue button.
    expect(
      await screen.findByText(/Discord isn't available right now/),
    ).toBeInTheDocument();
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Try again" }),
      ).toBeInTheDocument(),
    );
    expect(
      screen.queryByRole("link", { name: /Continue to Discord/ }),
    ).not.toBeInTheDocument();
  });
});
