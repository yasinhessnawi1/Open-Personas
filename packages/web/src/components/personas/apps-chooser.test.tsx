/**
 * N3 (MCP-as-apps) Task 4 — the apps chooser.
 *
 * Verifies the locked Phase-3 decisions are realized:
 *   - N3-D-5: a searchable directory of app cards → per-app detail (expand);
 *             decoupled from the raw tools view; enablement via `mcp:<name>`.
 *   - N3-D-6: the icon is a LOCAL glyph — NEVER a raw <img> to icon_url.
 *   - N3-D-7: ONE honest capability line — no enumerated tool names, no count.
 *   - N3-D-8: compact trust signal on the card; full disclosure in the detail.
 *   - N3-D-9/10: state rendering + the read-honest needs-setup disclosure.
 */

import { fireEvent, render, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it, vi } from "vitest";
import messages from "@/i18n/messages/en.json";
import { AppsChooser } from "./apps-chooser";
import type { McpConnectionStatus } from "./mcp-connection-label";
import type { McpCatalogEntry, McpCatalogSecret } from "./persona-form";

const secret: McpCatalogSecret = {
  name: "github.personal_access_token",
  env: "GITHUB_PERSONAL_ACCESS_TOKEN",
  example: "<YOUR_TOKEN>",
  description: "A GitHub PAT.",
};

function app(over: Partial<McpCatalogEntry> = {}): McpCatalogEntry {
  return {
    name: "thing",
    description: "does things",
    provider: "mcp:optional",
    defaultEnabled: false,
    requiredEnv: [],
    displayName: "",
    iconUrl: "",
    image: "",
    serverType: "builtin",
    risk: "low",
    sourceProject: "",
    sourceCommit: "",
    signed: false,
    allowHosts: [],
    secrets: [],
    ...over,
  };
}

function renderChooser(props: {
  apps: McpCatalogEntry[];
  declaredTools?: string[];
  unavailableMcpServers?: string[];
  connections?: McpConnectionStatus[];
  onChange?: (tools: string[]) => void;
}) {
  const onChange = props.onChange ?? vi.fn();
  const result = render(
    <NextIntlClientProvider locale="en" messages={messages}>
      <AppsChooser
        apps={props.apps}
        declaredTools={props.declaredTools ?? []}
        unavailableMcpServers={props.unavailableMcpServers ?? []}
        connections={props.connections}
        onChange={onChange}
      />
    </NextIntlClientProvider>,
  );
  return { ...result, onChange };
}

/** Expand a card's detail (Base UI Collapsible mounts the panel on open). */
function expand(card: HTMLElement) {
  const trigger = card.querySelector<HTMLButtonElement>(
    '[data-slot="collapsible-trigger"]',
  );
  fireEvent.click(trigger as HTMLButtonElement);
}

function cardFor(container: HTMLElement, text: string): HTMLElement {
  const card = Array.from(
    container.querySelectorAll<HTMLElement>('[data-slot="app-card"]'),
  ).find((c) => c.textContent?.includes(text));
  if (!card) throw new Error(`no app card for ${text}`);
  return card;
}

describe("AppsChooser", () => {
  it("renders one card per app with the friendly display name", () => {
    const { container } = renderChooser({
      apps: [
        app({ name: "github", displayName: "GitHub" }),
        app({ name: "time" }),
      ],
    });
    const cards = container.querySelectorAll('[data-slot="app-card"]');
    expect(cards.length).toBe(2);
    expect(cardFor(container, "GitHub")).toBeTruthy();
  });

  it("falls back to the raw name when displayName is empty", () => {
    const { container } = renderChooser({ apps: [app({ name: "time" })] });
    expect(cardFor(container, "time")).toBeTruthy();
  });

  it("N3-D-6: renders a LOCAL glyph — never an <img> to icon_url", () => {
    const { container } = renderChooser({
      apps: [app({ name: "github", iconUrl: "https://evil.test/x.png" })],
    });
    // No raw <img> anywhere, and certainly not pointed at the external host.
    expect(container.querySelector("img")).toBeNull();
    expect(container.innerHTML).not.toContain("evil.test");
    expect(container.querySelector('[data-slot="app-icon"]')).toBeTruthy();
  });

  it("N3-D-5: search filters the cards; an empty match shows the search-empty note", () => {
    const { container, getByRole } = renderChooser({
      apps: [
        app({ name: "github", displayName: "GitHub" }),
        app({ name: "time" }),
      ],
    });
    const search = getByRole("searchbox");
    fireEvent.change(search, { target: { value: "git" } });
    expect(container.querySelectorAll('[data-slot="app-card"]').length).toBe(1);
    expect(cardFor(container, "GitHub")).toBeTruthy();

    fireEvent.change(search, { target: { value: "zzz" } });
    expect(container.querySelectorAll('[data-slot="app-card"]').length).toBe(0);
    expect(
      container.querySelector('[data-slot="apps-search-empty"]'),
    ).toBeTruthy();
  });

  it("N3-D-7: shows ONE honest capability line — no enumerated tools, no count", () => {
    const { container } = renderChooser({ apps: [app({ name: "github" })] });
    const card = cardFor(container, "github");
    expand(card);
    const cap = card.querySelector('[data-slot="app-capability"]');
    expect(cap?.textContent).toBe(messages.apps.capability);
    // The line carries no digit (no fabricated count) and there is no tool list.
    expect(cap?.textContent).not.toMatch(/\d/);
    expect(card.querySelector('[data-slot="app-tools-list"]')).toBeNull();
  });

  it("N3-D-8: a compact trust signal on the card, full disclosure in the detail", () => {
    const { container } = renderChooser({
      apps: [
        app({
          name: "github",
          signed: true,
          image: "ghcr.io/x/github:1",
          sourceProject: "https://github.com/docker/mcp-registry",
          sourceCommit: "0123456789abcdef0123456789abcdef01234567",
          allowHosts: ["api.github.com"],
        }),
      ],
    });
    const card = cardFor(container, "github");
    // Card carries the compact signal even before expanding.
    expect(card.querySelector('[data-slot="app-trust-signal"]')).toBeTruthy();
    // Full provenance only after expanding into the detail.
    expect(card.querySelector('[data-slot="app-trust"]')).toBeNull();
    expand(card);
    const trust = card.querySelector('[data-slot="app-trust"]');
    expect(trust?.textContent).toContain("ghcr.io/x/github:1");
    expect(trust?.textContent).toContain("api.github.com");
    expect(trust?.textContent).toContain("docker/mcp-registry");
  });

  it("R9-039: shows the catalog's full real description, not a truncated one-liner", () => {
    const longDescription =
      "The Rust MCP Filesystem is a high-performance, asynchronous, and " +
      "lightweight Model Context Protocol server built in Rust for secure " +
      "and efficient filesystem operations. Designed with security in mind, " +
      "it operates in read-only mode by default and restricts clients from " +
      "updating allowed directories via MCP Roots unless explicitly enabled.";
    const { container } = renderChooser({
      apps: [
        app({ name: "rust-mcp-filesystem", description: longDescription }),
      ],
    });
    const card = cardFor(container, "rust-mcp-filesystem");
    // Not visible before expanding (the collapsed row keeps its truncated teaser).
    expect(card.querySelector('[data-slot="app-full-description"]')).toBeNull();
    expand(card);
    const full = card.querySelector('[data-slot="app-full-description"]');
    // The FULL text, byte for byte — never sliced/ellipsised.
    expect(full?.textContent).toBe(longDescription);
  });

  it("R9-039: an empty catalog description degrades to an honest fallback, never blank", () => {
    const { container } = renderChooser({
      apps: [app({ name: "sparse-server", description: "" })],
    });
    const card = cardFor(container, "sparse-server");
    expand(card);
    const full = card.querySelector('[data-slot="app-full-description"]');
    expect(full?.textContent).toBe(messages.apps.detail.noDescription);
    expect(full?.textContent).toBeTruthy();
  });

  it("R9-039: the capability line carries a heading — a labeled section, not a bare sentence", () => {
    const { container } = renderChooser({ apps: [app({ name: "github" })] });
    const card = cardFor(container, "github");
    expand(card);
    const heading = card.querySelector('[data-slot="app-capability-heading"]');
    expect(heading?.textContent).toBe(messages.apps.toolsHeading);
  });

  it("R9-039: structured trust rows carry labels + a real link; the boilerplate sentence is a footnote after the real content", () => {
    const { container } = renderChooser({
      apps: [
        app({
          name: "github",
          description: "GitHub repository, issue, and PR operations.",
          image: "ghcr.io/x/github:1",
          sourceProject: "https://github.com/docker/mcp-registry",
          sourceCommit: "0123456789abcdef0123456789abcdef01234567",
          allowHosts: ["api.github.com"],
        }),
      ],
    });
    const card = cardFor(container, "github");
    expand(card);

    // A REAL <a> link to the source — not plain text.
    const link = card.querySelector<HTMLAnchorElement>(
      '[data-slot="app-trust"] a',
    );
    expect(link).toBeTruthy();
    expect(link?.getAttribute("href")).toBe(
      "https://github.com/docker/mcp-registry",
    );
    expect(link?.getAttribute("rel")).toContain("noopener");

    // Labeled rows, not sentence-style prose.
    const rows = card.querySelectorAll('[data-slot="app-trust-row"]');
    expect(rows.length).toBeGreaterThanOrEqual(3); // runs + source + hosts

    // The generic "what an app is" sentence is present but demoted to a
    // footnote — a DISTINCT element from the real content, and it renders
    // strictly after the full description in document order.
    const footnote = card.querySelector('[data-slot="app-trust-footnote"]');
    expect(footnote?.textContent).toBe(messages.apps.trust.honest);
    const fullDescription = card.querySelector(
      '[data-slot="app-full-description"]',
    );
    expect(fullDescription?.textContent).toBeTruthy();
    const html = card.innerHTML;
    expect(html.indexOf('data-slot="app-full-description"')).toBeLessThan(
      html.indexOf('data-slot="app-trust-footnote"'),
    );
  });

  it("R9-039: the credential box only renders when the app declares a requirement", () => {
    const { container } = renderChooser({
      apps: [app({ name: "plain-no-secrets" })],
    });
    const card = cardFor(container, "plain-no-secrets");
    expand(card);
    expect(card.querySelector('[data-slot="app-needs-setup"]')).toBeNull();
  });

  it("renders the four states via deriveAppState", () => {
    const { container } = renderChooser({
      apps: [
        app({ name: "plain" }), // available
        app({ name: "github", secrets: [secret] }), // needs-setup
        app({ name: "time" }), // enabled (via tools)
        app({ name: "gone" }), // unavailable
      ],
      declaredTools: ["mcp:time"],
      unavailableMcpServers: ["gone"],
    });
    const state = (name: string) =>
      cardFor(container, name).getAttribute("data-state");
    expect(state("plain")).toBe("available");
    expect(state("github")).toBe("needs-setup");
    expect(state("time")).toBe("enabled");
    expect(state("gone")).toBe("unavailable");
  });

  it("N3-D-10: needs-setup detail is read-honest — declares + deployment-level, no toggle-as-form", () => {
    const { container } = renderChooser({
      apps: [app({ name: "github", secrets: [secret] })],
    });
    const card = cardFor(container, "github");
    expand(card);
    const note = card.querySelector('[data-slot="app-needs-setup"]');
    expect(note).toBeTruthy();
    expect(note?.textContent?.toLowerCase()).toContain("deployment level");
    expect(note?.textContent).toContain(secret.env);
    // It is informational text, NOT an input/form field.
    expect(note?.querySelector("input")).toBeNull();
  });

  it("enabling an app writes mcp:<name>; disabling removes it (preserving other tools)", () => {
    const onChange = vi.fn();
    const { container } = renderChooser({
      apps: [app({ name: "github" })],
      declaredTools: ["web_search"],
      onChange,
    });
    const card = cardFor(container, "github");
    expand(card);
    fireEvent.click(
      card.querySelector('[data-slot="app-toggle"]') as HTMLElement,
    );
    expect(onChange).toHaveBeenCalledWith(["web_search", "mcp:github"]);
  });

  it("does not show an enable toggle for an unavailable app (no re-add action)", () => {
    const { container } = renderChooser({
      apps: [app({ name: "gone" })],
      declaredTools: ["mcp:gone"],
      unavailableMcpServers: ["gone"],
    });
    const card = cardFor(container, "gone");
    expand(card);
    expect(card.querySelector('[data-slot="app-toggle"]')).toBeNull();
    expect(card.querySelector('[data-slot="app-unavailable"]')).toBeTruthy();
  });

  it("shows the empty-state when the catalog is empty", () => {
    const { getByText } = renderChooser({ apps: [] });
    expect(getByText(messages.apps.empty)).toBeTruthy();
  });

  it("provides an accessible label per app (open detail)", () => {
    const { container } = renderChooser({
      apps: [app({ name: "github", displayName: "GitHub" })],
    });
    const card = cardFor(container, "GitHub");
    const trigger = within(card).getByLabelText(
      messages.apps.open.replace("{name}", "GitHub"),
    );
    expect(trigger).toBeTruthy();
  });

  // N6 merge-back — the per-server connection badge on the edit cards.
  describe("connection badge", () => {
    const connBadge = (card: HTMLElement) =>
      card.querySelector('[data-slot="mcp-connection-badge"]');

    it("renders the friendly badge per status next to the state badge", () => {
      const { container } = renderChooser({
        apps: [
          app({ name: "github", displayName: "GitHub" }),
          app({ name: "time" }),
          app({ name: "fetch" }),
        ],
        declaredTools: ["mcp:github", "mcp:time", "mcp:fetch"],
        connections: [
          { server_name: "github", connected: true, reason: null },
          { server_name: "time", connected: false, reason: "starting" },
          { server_name: "fetch", connected: false, reason: "no_key" },
        ],
      });
      expect(connBadge(cardFor(container, "GitHub"))?.textContent).toBe(
        messages.apps.connection.connected,
      );
      expect(connBadge(cardFor(container, "time"))?.textContent).toBe(
        messages.apps.connection.starting,
      );
      expect(connBadge(cardFor(container, "fetch"))?.textContent).toBe(
        messages.apps.connection.noKey,
      );
      // The state badge is still there — connection is shown NEXT to it.
      expect(
        cardFor(container, "GitHub").querySelector(
          '[data-slot="app-state-badge"]',
        )?.textContent,
      ).toBe(messages.apps.state.enabled);
      // Never the raw enum.
      expect(container.textContent).not.toContain("no_key");
    });

    it("renders NO badge for an app without a status row", () => {
      const { container } = renderChooser({
        apps: [app({ name: "github" }), app({ name: "time" })],
        connections: [
          { server_name: "time", connected: false, reason: "stopped" },
        ],
      });
      expect(connBadge(cardFor(container, "github"))).toBeNull();
      expect(connBadge(cardFor(container, "time"))).not.toBeNull();
    });

    it("fail-soft: no connections prop (fetch failed / older api) renders clean", () => {
      const { container } = renderChooser({
        apps: [app({ name: "github" })],
        declaredTools: ["mcp:github"],
      });
      expect(
        container.querySelector('[data-slot="mcp-connection-badge"]'),
      ).toBeNull();
      // The card itself is untouched.
      expect(cardFor(container, "github")).toBeTruthy();
    });
  });
});
