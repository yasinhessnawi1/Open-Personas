/**
 * N6 merge-back — the profile surface's subtle not-connected hint.
 *
 * "What <persona> can do" stays clean on the happy path: a badge appears ONLY
 * for an MCP-sourced app whose runtime reports not-connected — connected apps
 * and built-in tools/skills render nothing, and an empty connections list
 * (older api / community / fetch failure) renders nothing (fail-soft).
 */

import { render } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it } from "vitest";
import messages from "@/i18n/messages/en.json";
import { McpConnectionHint } from "./mcp-connection-hint";
import type { McpConnectionStatus } from "./mcp-connection-label";

function renderHint(appId: string, connections: McpConnectionStatus[]) {
  return render(
    <NextIntlClientProvider locale="en" messages={messages}>
      <McpConnectionHint appId={appId} connections={connections} />
    </NextIntlClientProvider>,
  );
}

const badge = (c: HTMLElement) =>
  c.querySelector('[data-slot="mcp-connection-badge"]');

describe("McpConnectionHint", () => {
  it("shows the friendly badge for a not-connected MCP app", () => {
    const { container } = renderHint("mcp:github", [
      { server_name: "github", connected: false, reason: "no_key" },
    ]);
    const el = badge(container);
    expect(el).not.toBeNull();
    // Friendly label, never the raw enum.
    expect(el?.textContent).toBe("Needs setup");
    expect(container.textContent).not.toContain("no_key");
  });

  it("renders NOTHING for a connected MCP app (clean happy path)", () => {
    const { container } = renderHint("mcp:github", [
      { server_name: "github", connected: true, reason: null },
    ]);
    expect(container.textContent).toBe("");
  });

  it("renders NOTHING for a built-in (non-MCP) app id", () => {
    const { container } = renderHint("web_search", [
      { server_name: "web_search", connected: false, reason: "stopped" },
    ]);
    expect(container.textContent).toBe("");
  });

  it("renders NOTHING when the connections fetch failed (empty list)", () => {
    const { container } = renderHint("mcp:github", []);
    expect(container.textContent).toBe("");
  });

  it("renders NOTHING for an MCP app with no status row (unassigned)", () => {
    const { container } = renderHint("mcp:github", [
      { server_name: "time", connected: false, reason: "starting" },
    ]);
    expect(container.textContent).toBe("");
  });
});
