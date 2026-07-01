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
import type { PersonaDoc } from "@/lib/persona-draft";
import { PersonaEditor } from "./persona-editor";

// PersonaEditor renders PersonaForm (V6 VoiceSelector → useAuth + /v1/voices) and
// Spec-30 SuggestCapabilities / ByoMcpManager (useAuth). Mock both so the editor
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
