/**
 * Spec S3 — the Specialities chooser (T3–T6).
 *
 * Verifies the load-bearing Phase-3 decisions are realized:
 *   - see-then-grant: the enable/consent control lives ONLY in the expanded detail
 *     (no card-level quick-toggle); friction sits on enable, disable is one click.
 *   - the third-party consent flow: an honest disclosure (candidate A) precedes the
 *     grant; declining → not enabled; granting records consent + enables.
 *   - trust-tier labels per tier (positive-elevation); app-vs-speciality distinct glyph.
 *   - graceful states: unavailable tombstone; stale consent → needs-consent re-gate.
 */

import { fireEvent, render, waitFor, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it, vi } from "vitest";
import messages from "@/i18n/messages/en.json";
import type { SpecialityEntry } from "./speciality-state";

vi.mock("@/auth", () => ({ useAuth: () => ({ getToken: async () => null }) }));
vi.mock("@/lib/specialities/specialities", () => ({
  fetchSpecialities: vi.fn(),
  recordSpecialityConsent: vi.fn(),
}));

import {
  fetchSpecialities,
  recordSpecialityConsent,
} from "@/lib/specialities/specialities";
import { SpecialitiesChooser } from "./specialities-chooser";

function spec(over: Partial<SpecialityEntry> = {}): SpecialityEntry {
  return {
    name: "legal_research",
    description: "Legal research helper.",
    when_to_use: null,
    trust: "third_party",
    requires_consent: true,
    content_hash: "hash_v1",
    source: "github:acme/skills",
    source_uri: null,
    source_ref: null,
    consent_state: "none",
    ...over,
  };
}

const VETTED = spec({
  name: "code_review",
  trust: "vetted",
  requires_consent: false,
  consent_state: "not_required",
  source: "anthropic",
});

async function renderChooser(props: {
  specialities: SpecialityEntry[];
  declaredSkills?: string[];
  personaId?: string;
  onChange?: (skills: string[]) => void;
}) {
  vi.mocked(fetchSpecialities).mockResolvedValue(props.specialities);
  const onChange = props.onChange ?? vi.fn();
  const result = render(
    <NextIntlClientProvider locale="en" messages={messages}>
      <SpecialitiesChooser
        personaId={props.personaId ?? "persona_1"}
        declaredSkills={props.declaredSkills ?? []}
        onChange={onChange}
      />
    </NextIntlClientProvider>,
  );
  // Wait for the async catalog fetch to resolve into the rendered directory.
  await waitFor(() =>
    expect(
      result.container.querySelector('[data-slot="specialities-chooser"]'),
    ).toBeTruthy(),
  );
  return { ...result, onChange };
}

function cardFor(container: HTMLElement, name: string): HTMLElement {
  // Skill names are normalised to friendly labels in the card (R4 T5), so match
  // on the stable `data-speciality-name` id hook rather than the display copy.
  const card = Array.from(
    container.querySelectorAll<HTMLElement>('[data-slot="speciality-card"]'),
  ).find(
    (c) =>
      c.getAttribute("data-speciality-name") === name ||
      c.textContent?.includes(name),
  );
  if (!card) throw new Error(`no speciality card for ${name}`);
  return card;
}

function expand(card: HTMLElement) {
  const trigger = card.querySelector<HTMLButtonElement>(
    '[data-slot="collapsible-trigger"]',
  );
  fireEvent.click(trigger as HTMLButtonElement);
}

describe("SpecialitiesChooser — see-then-grant", () => {
  it("shows no enable control on the collapsed card; it appears only after expanding", async () => {
    const { container } = await renderChooser({ specialities: [VETTED] });
    const card = cardFor(container, "code_review");
    // Collapsed: no toggle / consent control is reachable.
    expect(card.querySelector('[data-slot="speciality-toggle"]')).toBeNull();
    expect(card.querySelector('[data-slot="speciality-consent"]')).toBeNull();
    expand(card);
    // Expanded: the enable control is now present (a non-gated speciality → plain toggle).
    expect(
      card.querySelector('[data-slot="speciality-toggle"]'),
    ).not.toBeNull();
  });

  it("disabling an enabled speciality is one click (no ceremony)", async () => {
    const { container, onChange } = await renderChooser({
      specialities: [VETTED],
      declaredSkills: ["code_review"],
    });
    const card = cardFor(container, "code_review");
    expand(card);
    fireEvent.click(
      card.querySelector(
        '[data-slot="speciality-toggle"]',
      ) as HTMLButtonElement,
    );
    expect(onChange).toHaveBeenCalledWith([]); // removed, immediately
  });
});

describe("SpecialitiesChooser — third-party consent flow", () => {
  it("a gated speciality shows the honest consent disclosure, not a plain toggle", async () => {
    const { container } = await renderChooser({ specialities: [spec()] });
    const card = cardFor(container, "legal_research");
    expand(card);
    const consent = card.querySelector('[data-slot="speciality-consent"]');
    expect(consent).not.toBeNull();
    expect(card.querySelector('[data-slot="speciality-toggle"]')).toBeNull();
    // The candidate-A honesty: instructions the persona follows + unreviewed + reversible.
    expect(consent?.textContent).toContain(
      "instructions your persona will follow",
    );
    expect(consent?.textContent).toContain("hasn't reviewed");
    expect(consent?.textContent).toContain("disable it anytime");
  });

  it("declining (not granting) never enables the speciality", async () => {
    const { container, onChange } = await renderChooser({
      specialities: [spec()],
    });
    expand(cardFor(container, "legal_research"));
    expect(onChange).not.toHaveBeenCalled();
  });

  it("granting consent records it and then enables the speciality", async () => {
    vi.mocked(recordSpecialityConsent).mockResolvedValue(
      spec({ consent_state: "granted" }),
    );
    const { container, onChange } = await renderChooser({
      specialities: [spec()],
    });
    const card = cardFor(container, "legal_research");
    expand(card);
    fireEvent.click(
      card.querySelector(
        '[data-slot="speciality-consent-grant"]',
      ) as HTMLButtonElement,
    );
    await waitFor(() =>
      expect(recordSpecialityConsent).toHaveBeenCalledWith(
        "persona_1",
        "legal_research",
        true,
        expect.any(Function),
      ),
    );
    await waitFor(() =>
      expect(onChange).toHaveBeenCalledWith(["legal_research"]),
    );
  });

  it("a vetted speciality enables with a plain toggle (no consent gate)", async () => {
    const { container, onChange } = await renderChooser({
      specialities: [VETTED],
    });
    const card = cardFor(container, "code_review");
    expand(card);
    expect(card.querySelector('[data-slot="speciality-consent"]')).toBeNull();
    fireEvent.click(
      card.querySelector(
        '[data-slot="speciality-toggle"]',
      ) as HTMLButtonElement,
    );
    expect(onChange).toHaveBeenCalledWith(["code_review"]);
  });

  it("a declared gated speciality with stale consent re-gates (needs-consent + re-gate copy)", async () => {
    const { container } = await renderChooser({
      specialities: [spec({ consent_state: "stale" })],
      declaredSkills: ["legal_research"],
    });
    const card = cardFor(container, "legal_research");
    expect(card.getAttribute("data-state")).toBe("needs-consent");
    expand(card);
    expect(
      card.querySelector('[data-slot="speciality-consent"]')?.textContent,
    ).toContain("changed since you enabled it");
  });
});

describe("SpecialitiesChooser — trust labels + states", () => {
  it("renders a distinct tier badge per tier (positive-elevation)", async () => {
    const { container } = await renderChooser({
      specialities: [
        VETTED,
        spec({
          name: "community_skill",
          trust: "community",
          source: "openclaw",
        }),
        spec({ name: "tp_skill", trust: "third_party", source: "github:x/y" }),
      ],
    });
    const tiers = Array.from(
      container.querySelectorAll('[data-slot="speciality-tier"]'),
    ).map((b) => b.getAttribute("data-tier"));
    expect(tiers).toContain("vetted");
    expect(tiers).toContain("community");
    expect(tiers).toContain("third_party");
    expect(
      within(cardFor(container, "code_review")).getByText(/Vetted/),
    ).toBeTruthy();
  });

  it("a declared speciality dropped from the catalog renders a graceful tombstone", async () => {
    const { container } = await renderChooser({
      specialities: [VETTED],
      declaredSkills: ["code_review", "removed_skill"],
    });
    const tomb = Array.from(
      container.querySelectorAll<HTMLElement>('[data-slot="speciality-card"]'),
    ).find((c) => c.getAttribute("data-state") === "unavailable");
    expect(tomb?.textContent).toContain("removed_skill");
    expect(tomb?.textContent).toContain("no longer available");
  });
});
