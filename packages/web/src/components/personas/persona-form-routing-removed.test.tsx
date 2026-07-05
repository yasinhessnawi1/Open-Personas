/**
 * Spec P9 (P9-D-5) — the routing tuning surface is GONE from the persona form.
 *
 * Successor to routing-section.test.tsx (removed with its component): asserts
 * the form renders NO routing controls (no section, no enable switch, no
 * preset chips, no budget caps) and that editing through the form never
 * authors routing config the user didn't write. The stored-block back-compat
 * half lives in persona-draft.test.ts; the pin-still-routes half is proven
 * server-side (test_loop_policy_routing.py / test_reply_producer_routing.py).
 */

import { fireEvent, render } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it, vi } from "vitest";
import messages from "@/i18n/messages/en.json";
import type { PersonaDoc } from "@/lib/persona-draft";
import { PersonaForm } from "./persona-form";

vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ getToken: async () => null }),
}));
vi.mock("@/lib/voice/voices", () => ({
  fetchVoices: async () => ({ provider: null, voices: [] }),
}));
vi.mock("@/lib/specialities/specialities", () => ({
  fetchSpecialities: async () => [],
  recordSpecialityConsent: async () => ({}),
}));

function renderForm(doc: PersonaDoc, onChange = vi.fn()) {
  const result = render(
    <NextIntlClientProvider locale="en" messages={messages}>
      <PersonaForm
        doc={doc}
        onChange={onChange}
        tools={[]}
        skills={[]}
        mcpServers={[]}
      />
    </NextIntlClientProvider>,
  );
  return { ...result, onChange };
}

describe("PersonaForm — no user-facing tier control remains (P9 criterion 5)", () => {
  it("renders no routing section or routing controls", () => {
    const { container } = renderForm({});
    for (const slot of [
      "routing-section",
      "routing-enable-switch",
      "routing-config",
      "routing-preset",
      "routing-weights",
      "routing-budget",
    ]) {
      expect(container.querySelector(`[data-slot="${slot}"]`)).toBeNull();
    }
  });

  it("a stored pinned routing block is invisible AND untouched by a form edit", () => {
    const doc: PersonaDoc = {
      schema_version: "1.0",
      identity: { name: "Old-Timer" },
      routing: {
        tier_for_generation: "mid",
        intelligent: { enabled: true },
      },
    };
    const { container, onChange } = renderForm(doc);
    // Invisible: nothing in the form surfaces the pin.
    expect(container.textContent).not.toContain("tier_for_generation");
    // Untouched: an ordinary identity edit emits a doc with the block verbatim.
    const name = container.querySelector<HTMLInputElement>(
      'input[value="Old-Timer"]',
    );
    expect(name).not.toBeNull();
    if (name) {
      fireEvent.change(name, { target: { value: "Renamed" } });
    }
    expect(onChange).toHaveBeenCalled();
    const emitted = onChange.mock.calls.at(-1)?.[0] as PersonaDoc;
    expect(emitted.routing).toEqual(doc.routing);
  });
});
