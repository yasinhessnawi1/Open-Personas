import { fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it, vi } from "vitest";
import en from "@/i18n/messages/en.json";
import {
  CONNECTOR_CATALOGUE,
  type ConnectorMeta,
} from "@/lib/connectors/catalogue";
import type { ConnectorConnection } from "@/lib/connectors/use-connectors";
import { ConnectorCard } from "./connector-card";

/**
 * Spec C6 (T4) — the connector card renders honest connected / not-connected state and the
 * connected identity per platform (C6-KL-1: id-based platforms show "ID <id>", never naked;
 * phone/email verbatim). The connect/disconnect callbacks fire on click (the flows wire them
 * in T5/T9).
 */

function meta(key: ConnectorMeta["key"]): ConnectorMeta {
  const found = CONNECTOR_CATALOGUE.find((m) => m.key === key);
  if (!found) throw new Error(`no catalogue entry for ${key}`);
  return found;
}

function conn(platform: string, identity: string): ConnectorConnection {
  return {
    platform,
    platform_identity: identity,
    linked_at: "2026-07-01T00:00:00Z",
  };
}

function renderCard(ui: React.ReactNode) {
  return render(
    <NextIntlClientProvider locale="en" messages={en}>
      {ui}
    </NextIntlClientProvider>,
  );
}

describe("ConnectorCard", () => {
  it("shows not-connected state + a Connect action when there is no binding", () => {
    const onConnect = vi.fn();
    renderCard(
      <ConnectorCard
        meta={meta("telegram")}
        connection={null}
        onConnect={onConnect}
      />,
    );
    expect(screen.getByText("Not connected")).toBeInTheDocument();
    const connect = screen.getByRole("button", { name: "Connect" });
    fireEvent.click(connect);
    expect(onConnect).toHaveBeenCalledWith(meta("telegram"));
  });

  it("shows a phone identity verbatim when connected (human-recognisable)", () => {
    renderCard(
      <ConnectorCard
        meta={meta("sms")}
        connection={conn("sms", "+4790000000")}
      />,
    );
    expect(screen.getByText("Connected")).toBeInTheDocument();
    expect(screen.getByText(/\+4790000000/)).toBeInTheDocument();
  });

  it("shows an id-based platform identity as 'ID <id>' — honest, not naked (C6-KL-1)", () => {
    renderCard(
      <ConnectorCard
        meta={meta("discord")}
        connection={conn("discord", "998877")}
      />,
    );
    expect(screen.getByText(/ID 998877/)).toBeInTheDocument();
  });

  it("fires onDisconnect with the binding when connected", () => {
    const onDisconnect = vi.fn();
    const binding = conn("email", "ada@example.com");
    renderCard(
      <ConnectorCard
        meta={meta("email")}
        connection={binding}
        onDisconnect={onDisconnect}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Disconnect" }));
    expect(onDisconnect).toHaveBeenCalledWith(binding);
  });

  it("shows a neutral checking state on first load (not a false 'not connected')", () => {
    renderCard(
      <ConnectorCard meta={meta("telegram")} connection={null} loading />,
    );
    expect(screen.getByText("Checking…")).toBeInTheDocument();
    expect(screen.queryByText("Not connected")).not.toBeInTheDocument();
  });
});
