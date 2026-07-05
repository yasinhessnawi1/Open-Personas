"use client";

import { useTranslations } from "next-intl";
import type { CSSProperties } from "react";
import { Stack } from "@/components/layout";
import { PersonaAvatar } from "@/components/persona/persona-avatar";
import {
  ACCENT_OKLCH,
  PERSONA_EXAMPLE_CATEGORIES,
  type PersonaExample,
  type PersonaExampleCategory,
} from "@/lib/persona-examples";
import { derivePersonaIdentityColor } from "@/lib/persona-identity";
import { cn } from "@/lib/utils";

/**
 * Starter-persona gallery for the new-persona page (Spec 36 design match).
 *
 * Each card matches the design prototype's `.starter`: a per-persona
 * identity-coloured avatar (initials in Fraunces) + name + truncated role, in a
 * compact auto-fill grid — not the old editorial card. Picking one hands the
 * full example to `onSelect`; the wizard opens it as an editable structured
 * draft for direct create. This component is presentation + one callback; it
 * never calls the API.
 *
 * Colour discipline (no-literals gate, D-F2-6): the per-category accent rail
 * uses pre-composed `--accent*` custom properties; each card's avatar + hover/
 * selected ring use the persona's derived identity colour exposed as an inline
 * `--v-id` custom property (mirrors the design). Class names reference both via
 * `var(--…)`, which does not trip the colour-literal regex.
 */

export const CATEGORY_LABEL_KEY: Record<PersonaExampleCategory["id"], string> =
  {
    work: "gallery.categoryWork",
    learning: "gallery.categoryLearning",
    creative: "gallery.categoryCreative",
    wellness: "gallery.categoryWellness",
    experts: "gallery.categoryExperts",
    companionship: "gallery.categoryCompanionship",
    mentors: "gallery.categoryMentors",
    companions: "gallery.categoryCompanions",
    voices: "gallery.categoryVoices",
    "life-coaches": "gallery.categoryLifeCoaches",
  };

/**
 * Pre-composed accent custom properties for a category rail. `--accent` is the
 * solid rail; `--accent-faint` (12% alpha) backs the heading dot.
 */
function accentStyle(accent: PersonaExampleCategory["accent"]): CSSProperties {
  const { h, c, l } = ACCENT_OKLCH[accent];
  return {
    "--accent": `oklch(${l} ${c} ${h})`,
    "--accent-faint": `oklch(${l} ${c} ${h} / 0.12)`,
  } as CSSProperties;
}

export function ExampleGallery({
  onSelect,
  selectedId,
}: {
  /** Called with the chosen example when a card is picked. */
  onSelect: (example: PersonaExample) => void;
  /** Id of the most recently picked example, for the selected affordance. */
  selectedId?: string | null;
}) {
  const t = useTranslations("author");

  return (
    <section data-slot="example-gallery" aria-label={t("gallery.ownPathLabel")}>
      <Stack gap={8}>
        {PERSONA_EXAMPLE_CATEGORIES.map((category, categoryIndex) => (
          <CategorySection
            key={category.id}
            category={category}
            label={t(CATEGORY_LABEL_KEY[category.id])}
            useNamed={(name) => t("gallery.useNamed", { name })}
            selectedLabel={t("gallery.selected")}
            categoryIndex={categoryIndex}
            onSelect={onSelect}
            selectedId={selectedId}
          />
        ))}
      </Stack>
    </section>
  );
}

function CategorySection({
  category,
  label,
  useNamed,
  selectedLabel,
  categoryIndex,
  onSelect,
  selectedId,
}: {
  category: PersonaExampleCategory;
  label: string;
  useNamed: (name: string) => string;
  selectedLabel: string;
  categoryIndex: number;
  onSelect: (example: PersonaExample) => void;
  selectedId?: string | null;
}) {
  return (
    <div
      data-slot="example-category"
      style={accentStyle(category.accent)}
      className="flex flex-col gap-3"
    >
      <div className="flex items-center gap-3">
        <span
          aria-hidden="true"
          className="h-4 w-1 rounded-full bg-[var(--accent)]"
        />
        <h2 className="type-heading">{label}</h2>
      </div>
      <ul className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {category.examples.map((example, exampleIndex) => (
          <li key={example.id}>
            <ExampleCard
              example={example}
              useNamed={useNamed}
              selectedLabel={selectedLabel}
              isSelected={selectedId === example.id}
              // Stagger reveal across the page (capped) — calm load choreography.
              index={categoryIndex * 4 + exampleIndex}
              onSelect={onSelect}
            />
          </li>
        ))}
      </ul>
    </div>
  );
}

function ExampleCard({
  example,
  useNamed,
  selectedLabel,
  isSelected,
  index,
  onSelect,
}: {
  example: PersonaExample;
  useNamed: (name: string) => string;
  selectedLabel: string;
  isSelected: boolean;
  index: number;
  onSelect: (example: PersonaExample) => void;
}) {
  // Per-persona identity colour (matches the design's `--v-id`), used for the
  // avatar fill (via PersonaAvatar, same derivation) + the hover/selected ring.
  const idColor = derivePersonaIdentityColor({ id: example.id }).oklch;
  return (
    <button
      type="button"
      onClick={() => onSelect(example)}
      data-slot="example-card"
      data-selected={isSelected ? "true" : undefined}
      aria-label={useNamed(example.name)}
      style={
        {
          "--v-id": idColor,
          // animation-delay is a motion knob, not a colour/size literal; the
          // global reduced-motion path zeroes the duration so it stays inert.
          animationDelay: `${Math.min(index, 8) * 40}ms`,
        } as CSSProperties
      }
      className={cn(
        "motion-safe:fade-in-0 motion-safe:slide-in-from-bottom-1 group/example flex w-full items-center gap-3 rounded-xl bg-card p-3.5 text-left ring-1 ring-foreground/10 outline-none transition-[transform,box-shadow] duration-[var(--motion-duration-normal)] ease-[var(--motion-ease-emphasized)] motion-safe:animate-in",
        "hover:-translate-y-0.5 hover:shadow-[var(--elevation-2)] hover:ring-[var(--v-id)]",
        "focus-visible:ring-2 focus-visible:ring-[var(--v-id)] focus-visible:ring-offset-2 focus-visible:ring-offset-background",
        "data-[selected=true]:ring-2 data-[selected=true]:ring-[var(--v-id)]",
      )}
    >
      <PersonaAvatar
        persona={{ id: example.id, name: example.name }}
        size="md"
      />
      <span className="min-w-0 flex-1">
        <span className="type-heading block truncate leading-tight">
          {example.name}
        </span>
        <span className="type-ui block truncate text-muted-foreground">
          {example.role}
        </span>
      </span>
      {isSelected ? (
        <span className="type-caption shrink-0 font-mono text-[var(--v-id)] uppercase">
          {selectedLabel}
        </span>
      ) : null}
    </button>
  );
}
