/**
 * Spec 30 T11 — the unified capability section's MCP servers + cap note.
 *
 * Uses the real `author` message namespace so every form key resolves; asserts
 * MCP chips render, toggling writes an `mcp:<name>` entry into the persona's
 * tools (composing with the existing tools/skills chips), and the combined-cap
 * note reflects the count.
 */

import { fireEvent, render } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it, vi } from "vitest";
import messages from "@/i18n/messages/en.json";
import type { PersonaDoc } from "@/lib/persona-draft";
import { type McpCatalogEntry, PersonaForm } from "./persona-form";

// PersonaForm now renders the V6 VoiceSelector (useAuth + /v1/voices). Mock both
// so the MCP-focused tests render without a ClerkProvider or a network call.
vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ getToken: async () => null }),
}));
vi.mock("@/lib/voice/voices", () => ({
  fetchVoices: async () => ({ provider: null, voices: [] }),
}));
// Spec S3 — PersonaForm now renders the self-fetching SpecialitiesChooser; keep it
// offline so these MCP-focused tests stay deterministic.
vi.mock("@/lib/specialities/specialities", () => ({
  fetchSpecialities: async () => [],
  recordSpecialityConsent: async () => ({}),
}));

// N3 widened `McpCatalogEntry` with the Docker catalog-mirror display/trust/
// secrets fields. These MCP-section tests only exercise name/provider/
// defaultEnabled/requiredEnv, so a factory fills the additive fields with the
// same empty defaults `mapMcpCatalog` produces — keeping the fixtures focused.
const mcpEntry = (e: Partial<McpCatalogEntry>): McpCatalogEntry => ({
  name: "",
  description: "",
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
  ...e,
});

const MCP: McpCatalogEntry[] = [
  mcpEntry({
    name: "time",
    description: "current time",
    provider: "mcp:builtin",
    defaultEnabled: true,
  }),
  mcpEntry({
    name: "github",
    description: "GitHub ops",
    provider: "mcp:optional",
    requiredEnv: ["GITHUB_TOKEN"],
  }),
];

function renderForm(doc: PersonaDoc, onChange = vi.fn()) {
  const result = render(
    <NextIntlClientProvider locale="en" messages={messages}>
      <PersonaForm
        doc={doc}
        onChange={onChange}
        tools={["web_search"]}
        skills={["web_research"]}
        mcpServers={MCP}
      />
    </NextIntlClientProvider>,
  );
  return { ...result, onChange };
}

// N3 reframed the MCP section as the apps chooser (AppsChooser). The form-level
// contract is unchanged: one app per catalog server + the `mcp:<name>` tools
// mechanism + the combined-cap note. These assertions move to the chooser's
// card/toggle structure.
describe("PersonaForm — apps section (N3, reframed from spec 30 T11)", () => {
  it("renders an app card per catalog server", () => {
    const { container } = renderForm({});
    const cards = container.querySelectorAll('[data-slot="app-card"]');
    expect(cards.length).toBe(2);
  });

  it("marks an enabled app (mcp:<name> in tools) with the enabled state", () => {
    const { container } = renderForm({ tools: ["mcp:time"] });
    const enabled = container.querySelector(
      '[data-slot="app-card"][data-state="enabled"]',
    );
    expect(enabled?.textContent).toContain("time");
  });

  it("toggling an app writes mcp:<name> into tools (preserving other tools)", () => {
    const { container, onChange } = renderForm({ tools: ["web_search"] });
    // The enable toggle lives in the per-app detail (N3-D-5 directory → detail).
    // Base UI Collapsible mounts its panel on open, so expand the github card
    // first, then click its enable toggle.
    const githubCard = Array.from(
      container.querySelectorAll<HTMLElement>('[data-slot="app-card"]'),
    ).find((c) => c.textContent?.includes("github"));
    const trigger = githubCard?.querySelector<HTMLButtonElement>(
      '[data-slot="collapsible-trigger"]',
    );
    fireEvent.click(trigger as HTMLButtonElement);
    const toggle = githubCard?.querySelector<HTMLButtonElement>(
      '[data-slot="app-toggle"]',
    );
    expect(toggle).toBeTruthy();
    fireEvent.click(toggle as HTMLButtonElement);
    expect(onChange).toHaveBeenCalledTimes(1);
    const nextDoc = onChange.mock.calls[0][0] as PersonaDoc;
    expect(nextDoc.tools).toEqual(["web_search", "mcp:github"]);
  });

  it("shows the combined capability count (tools incl. mcp + skills)", () => {
    const { container } = renderForm({
      tools: ["web_search", "mcp:time"],
      skills: ["web_research"],
    });
    const note = container.querySelector('[data-slot="capability-count"]');
    expect(note?.textContent).toContain("3"); // 2 tools + 1 skill
  });
});
