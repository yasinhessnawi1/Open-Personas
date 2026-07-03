import { fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { beforeEach, describe, expect, it, vi } from "vitest";
import en from "@/i18n/messages/en.json";
import {
  CONNECTOR_CATALOGUE,
  type ConnectorMeta,
} from "@/lib/connectors/catalogue";
import type { ConnectorLinkArtifact } from "@/lib/connectors/connect-flow-machine";
import { CodeStep } from "./code-step";

/**
 * Spec C6 (T8) — the reversed phone/email code step: the code + the destination together, an
 * honest per-mechanism instruction, copy on each half, and the code copied VERBATIM (the
 * canonical form the server matches — bar 4).
 */

function meta(key: ConnectorMeta["key"]): ConnectorMeta {
  const found = CONNECTOR_CATALOGUE.find((m) => m.key === key);
  if (!found) throw new Error(`no catalogue entry for ${key}`);
  return found;
}

function artifact(
  over: Partial<ConnectorLinkArtifact> = {},
): ConnectorLinkArtifact {
  return {
    code: "ABCD1234",
    destination: "+15551234567",
    expires_at: "2099-01-01T00:00:00+00:00",
    deep_link: null,
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

describe("CodeStep", () => {
  const writeText = vi.fn().mockResolvedValue(undefined);
  beforeEach(() => {
    writeText.mockClear();
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
    });
  });

  it("shows the code AND the destination with a text-this-code instruction (SMS)", () => {
    renderWithIntl(<CodeStep artifact={artifact()} meta={meta("sms")} />);
    // Both halves are present.
    expect(screen.getByText("ABCD1234")).toBeInTheDocument();
    expect(screen.getByText("+15551234567")).toBeInTheDocument();
    expect(
      screen.getByText("Text this code to +15551234567 to finish linking."),
    ).toBeInTheDocument();
  });

  it("uses the email instruction for the email platform", () => {
    renderWithIntl(
      <CodeStep
        artifact={artifact({ destination: "persona@openpersona.app" })}
        meta={meta("email")}
      />,
    );
    expect(
      screen.getByText(
        "Email this code to persona@openpersona.app to finish linking.",
      ),
    ).toBeInTheDocument();
  });

  it("copies the code VERBATIM (canonical) — display letter-spacing never alters it", () => {
    renderWithIntl(<CodeStep artifact={artifact()} meta={meta("whatsapp")} />);
    const codeEl = document.querySelector('[data-slot="connect-code"]');
    expect(codeEl?.textContent).toBe("ABCD1234"); // no inserted spaces in the DOM text
    fireEvent.click(screen.getByRole("button", { name: "Copy code" }));
    expect(writeText).toHaveBeenCalledWith("ABCD1234"); // canonical, server-matchable
  });

  it("copies the destination on its own affordance (the user needs both halves)", () => {
    renderWithIntl(<CodeStep artifact={artifact()} meta={meta("sms")} />);
    fireEvent.click(screen.getByRole("button", { name: "Copy" }));
    expect(writeText).toHaveBeenCalledWith("+15551234567");
  });
});
