/**
 * Spec 31 T6 — <PersonaEditor> autonomy/consent wiring (D-31-X-autonomy-placement).
 *
 * The autonomy selector + consent toggle surface ONLY when editing an existing
 * persona (personaId + onConsentChange present) — never in the create wizard,
 * where a consent PATCH has no persisted persona to target. Toggling consent
 * calls the injected handler; autonomy rides the doc.
 */
import { fireEvent, render, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it, vi } from "vitest";
import messages from "@/i18n/messages/en.json";
import { getModels } from "@/lib/api/models-client";
import type { PersonaDoc } from "@/lib/persona-draft";
import { PersonaEditor } from "./persona-editor";

// PersonaEditor renders PersonaForm (V6 VoiceSelector → useAuth + /v1/voices) and
// Spec-30 SuggestCapabilities / ByoMcpManager (useAuth), plus its OWN Model
// section (Spec M1, M1-T7 → useAuth + /v1/models). Mock all three so the editor
// renders without a ClerkProvider or a network call (mirrors persona-form-mcp.test).
// vitest hoists vi.mock above the imports above.
vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ getToken: async () => null }),
}));
vi.mock("@/lib/voice/voices", () => ({
  fetchVoices: async () => ({ provider: null, voices: [] }),
}));
// Spec S3 — PersonaForm now renders the self-fetching SpecialitiesChooser; keep it
// offline so these editor tests stay deterministic.
vi.mock("@/lib/specialities/specialities", () => ({
  fetchSpecialities: async () => [],
  recordSpecialityConsent: async () => ({}),
}));
// Spec M1 (M1-T7) — the Model section self-fetches the catalog; default empty
// (fail-open) so these tests stay deterministic. Individual tests override via
// `vi.mocked(getModels).mockResolvedValueOnce(...)`.
vi.mock("@/lib/api/models-client", () => ({
  getModels: vi.fn(async () => []),
}));
// ByoMcpManager (Spec 30 T12) has its own dedicated test file
// (byo-mcp-manager.test.tsx) with its own API-client mocking; stub it here so
// these editor-level tests stay isolated from its self-fetch (mirrors
// author-wizard.test.tsx stubbing the whole PersonaEditor for the same
// reason — a parent's tests shouldn't exercise a child's own network calls).
vi.mock("./byo-mcp-manager", () => ({
  ByoMcpManager: () => null,
}));

const DOC: PersonaDoc = {
  schema_version: "1.0",
  identity: {
    name: "Astrid",
    role: "assistant",
    background: "",
    constraints: [],
  },
  self_facts: [],
  worldview: [],
  tools: [],
  skills: [],
  autonomy: "cautious",
};

function renderEditor(
  props: Partial<React.ComponentProps<typeof PersonaEditor>>,
) {
  const onSave = vi.fn(async () => undefined);
  return render(
    <NextIntlClientProvider locale="en" messages={messages}>
      <PersonaEditor
        initialDoc={DOC}
        tools={[]}
        skills={[]}
        onSave={onSave}
        saveLabel="Save"
        {...props}
      />
    </NextIntlClientProvider>,
  );
}

describe("PersonaEditor — autonomy/consent gating (Spec 31 T6)", () => {
  it("omits the autonomy/consent section in the create wizard (no personaId)", () => {
    const { container } = renderEditor({});
    expect(
      container.querySelector('[data-slot="autonomy-consent-section"]'),
    ).toBeNull();
  });

  it("surfaces the autonomy/consent section when editing an existing persona", () => {
    const onConsentChange = vi.fn(async () => undefined);
    const { container } = renderEditor({
      personaId: "p1",
      initialConsent: null,
      onConsentChange,
    });
    expect(
      container.querySelector('[data-slot="autonomy-consent-section"]'),
    ).not.toBeNull();
  });

  it("toggling consent calls onConsentChange(true)", async () => {
    const onConsentChange = vi.fn(async () => undefined);
    const { container } = renderEditor({
      personaId: "p1",
      initialConsent: null,
      onConsentChange,
    });
    const sw = container.querySelector(
      '[data-slot="consent-switch"]',
    ) as HTMLElement;
    fireEvent.click(sw);
    await waitFor(() => expect(onConsentChange).toHaveBeenCalledWith(true));
  });

  it("reverts optimistic consent state when the handler returns an error", async () => {
    const onConsentChange = vi.fn(async () => ({ error: "boom" }));
    const { container } = renderEditor({
      personaId: "p1",
      initialConsent: null,
      onConsentChange,
    });
    const sw = container.querySelector(
      '[data-slot="consent-switch"]',
    ) as HTMLElement;
    fireEvent.click(sw);
    await waitFor(() => expect(sw).toHaveAttribute("aria-checked", "false"));
  });
});

describe("PersonaEditor — Model section (Spec M1, M1-T7)", () => {
  it("renders the Model section in the create wizard too (no personaId gate)", () => {
    const { container } = renderEditor({});
    expect(
      container.querySelector('[data-slot="model-picker-trigger"]'),
    ).not.toBeNull();
  });

  it("renders the Model section when editing an existing persona", () => {
    const { container } = renderEditor({
      personaId: "p1",
      initialConsent: null,
      onConsentChange: vi.fn(async () => undefined),
    });
    expect(
      container.querySelector('[data-slot="model-picker-trigger"]'),
    ).not.toBeNull();
  });

  it("picking a model and saving carries routing.preferred_model into the saved YAML", async () => {
    vi.mocked(getModels).mockResolvedValueOnce([
      {
        id: "anthropic/claude-sonnet-4.6",
        label: "Claude Sonnet 4.6",
        provider: "anthropic",
        input_price_per_1m: 3,
        output_price_per_1m: 15,
        context_length: 200000,
        tools_supported: true,
        recommended: true,
      },
    ]);
    const onSave = vi.fn(async (_yaml: string) => undefined);
    const { container, getByRole } = renderEditor({ onSave });

    const trigger = container.querySelector(
      '[data-slot="model-picker-trigger"]',
    ) as HTMLElement;
    fireEvent.click(trigger);
    await waitFor(() => {
      expect(
        getByRole("menuitem", { name: /Claude Sonnet 4\.6/ }),
      ).toBeInTheDocument();
    });
    fireEvent.click(getByRole("menuitem", { name: /Claude Sonnet 4\.6/ }));

    fireEvent.click(getByRole("button", { name: "Save" }));
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    const yaml = onSave.mock.calls[0]?.[0] as string;
    expect(yaml).toContain("preferred_model: anthropic/claude-sonnet-4.6");
  });

  it("leaves routing untouched when the user never opens the Model section", async () => {
    const onSave = vi.fn(async (_yaml: string) => undefined);
    const { getByRole } = renderEditor({ onSave });
    fireEvent.click(getByRole("button", { name: "Save" }));
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    const yaml = onSave.mock.calls[0]?.[0] as string;
    expect(yaml).not.toContain("preferred_model");
  });
});
