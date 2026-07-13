/**
 * The canonical starter-persona roster (Spec 36, roster v2).
 *
 * Assembled from one module per category; `featured` leads (the flagship
 * shelf), then the categories in browse order: the company you can hire,
 * learning, life and style, wellness, creative, experts, mentors, companions,
 * and voices. See ./schema.ts for the shared contract and the honesty-rule
 * palettes; persona-examples.test.ts enforces dataset integrity.
 */

import { COMPANIONS_CATEGORY } from "./companions";
import { COMPANY_CATEGORY } from "./company";
import { CREATIVE_CATEGORY } from "./creative";
import { EXPERTS_CATEGORY } from "./experts";
import { FEATURED_CATEGORY } from "./featured";
import { LEARNING_CATEGORY } from "./learning";
import { LIFESTYLE_CATEGORY } from "./life-style";
import { MENTORS_CATEGORY } from "./mentors";
import type { PersonaExample, PersonaExampleCategory } from "./schema";
import { VOICES_CATEGORY } from "./voices";
import { WELLNESS_CATEGORY } from "./wellness";

export {
  ACCENT_OKLCH,
  type EpistemicStatus,
  MCP_PALETTE,
  type PersonaExample,
  type PersonaExampleCategory,
  type PersonaStructure,
  type SelfFactSeed,
  SKILL_PALETTE,
  TOOL_PALETTE,
  WIRABLE_TOOLS,
  type WorldviewSeed,
} from "./schema";

/** The curated starter set, featured shelf first (order is intentional). */
export const PERSONA_EXAMPLE_CATEGORIES: readonly PersonaExampleCategory[] = [
  FEATURED_CATEGORY,
  COMPANY_CATEGORY,
  LEARNING_CATEGORY,
  LIFESTYLE_CATEGORY,
  WELLNESS_CATEGORY,
  CREATIVE_CATEGORY,
  EXPERTS_CATEGORY,
  MENTORS_CATEGORY,
  COMPANIONS_CATEGORY,
  VOICES_CATEGORY,
];

/** Flat lookup covering every example (React keys + selection signal). */
export const PERSONA_EXAMPLES_BY_ID: Readonly<Record<string, PersonaExample>> =
  Object.fromEntries(
    PERSONA_EXAMPLE_CATEGORIES.flatMap((category) =>
      category.examples.map((example) => [example.id, example]),
    ),
  );
