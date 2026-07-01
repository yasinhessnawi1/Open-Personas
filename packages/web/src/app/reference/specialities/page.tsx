"use client";

/**
 * Spec S3 — Reference composition: the Specialities chooser.
 *
 * Fixture-fed static render (not live) of every state the chooser presents, so the
 * see-then-grant interaction, the honest third-party consent copy, and the trust-tier
 * labels can be judged in a real browser (light + dark). Mirrors the F1 reference-route
 * convention: no app shell, no auth, its own header.
 *
 * What to look at:
 *   - App-vs-speciality: a knowledge glyph + "expertise your persona will follow",
 *     a trust-TIER badge (not signed/risk) — legibly a different kind of thing than an app.
 *   - See-then-grant: the enable/consent control lives only in the expanded detail.
 *   - Consent honesty (third-party): names what it is, where it's from, that it's
 *     unreviewed, de-alarms, and is reversible — honest, not alarmist.
 *   - Tier labels: an earned "Vetted"; calm-honest Community / Third-party (no red alarm).
 *   - Graceful states: enabled, needs-consent (re-gate), unavailable tombstone.
 */

import { NextIntlClientProvider } from "next-intl";
import { SpecialitiesChooser } from "@/components/personas/specialities-chooser";
import type { SpecialityEntry } from "@/components/personas/speciality-state";
import messages from "@/i18n/messages/en.json";

function entry(over: Partial<SpecialityEntry>): SpecialityEntry {
  return {
    name: "skill",
    description: "",
    when_to_use: null,
    trust: "third_party",
    requires_consent: true,
    content_hash: "hash_v1",
    source: null,
    source_uri: null,
    source_ref: null,
    consent_state: "none",
    ...over,
  };
}

const FIXTURES: SpecialityEntry[] = [
  entry({
    name: "code_review",
    description: "Reviews code for correctness, style, and security.",
    when_to_use: "Reviewing a pull request or a code change.",
    trust: "vetted",
    requires_consent: false,
    consent_state: "not_required",
    source: "anthropic",
    source_ref: "a1b2c3d4e5f6",
  }),
  entry({
    name: "data_analysis",
    description: "Explores datasets and explains the findings.",
    when_to_use: "Making sense of a spreadsheet or a table.",
    trust: "community",
    consent_state: "none",
    source: "openclaw",
  }),
  entry({
    name: "legal_research",
    description: "Finds and summarises relevant case law.",
    when_to_use: "Researching a legal question.",
    trust: "third_party",
    consent_state: "none",
    source: "github:acme/legal-skills",
    source_ref: "9f8e7d6c5b4a",
  }),
  entry({
    name: "market_analysis",
    description: "Analyses a market and its competitive landscape.",
    trust: "third_party",
    consent_state: "granted",
    source: "github:acme/market-skills",
  }),
  entry({
    name: "brand_voice",
    description: "Writes in a consistent brand voice.",
    trust: "third_party",
    consent_state: "stale",
    source: "github:acme/brand-skills",
  }),
];

const DECLARED = ["market_analysis", "brand_voice", "retired_skill"];

export default function SpecialitiesReferencePage() {
  return (
    <div className="space-y-8">
      <header className="space-y-2">
        <p className="type-caption text-muted-foreground">
          Spec S3 · Specialities
        </p>
        <h1 className="type-display">Specialities</h1>
        <p className="type-body text-muted-foreground max-w-prose">
          Skills your persona can follow, surfaced with trust tiers and an
          honest consent flow for third-party content. Expand a card to see its
          detail before enabling it (see-then-grant); a community or third-party
          speciality asks for consent first.
        </p>
      </header>
      <section className="max-w-2xl">
        <NextIntlClientProvider locale="en" messages={messages}>
          <SpecialitiesChooser
            personaId="reference"
            declaredSkills={DECLARED}
            onChange={() => {}}
            initialItems={FIXTURES}
          />
        </NextIntlClientProvider>
      </section>
    </div>
  );
}
