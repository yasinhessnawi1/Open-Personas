// Client-safe i18n constants (no server-only imports), so both the server
// request config and client components (the Settings language switcher) can use
// them. The pseudo-locale "xx" exercises acceptance #9.
export const DEFAULT_LOCALE = "en";
export const LOCALES = ["en", "xx"] as const;
export const LOCALE_COOKIE = "NEXT_LOCALE";

/**
 * The "xx" pseudo-locale exists ONLY to exercise i18n string coverage
 * (acceptance #9, accented/bracketed placeholder text) — it is never a real
 * spoken language and must never reach a speech provider as a language code.
 */
export const PSEUDO_LOCALE: (typeof LOCALES)[number] = "xx";

/**
 * Locales that double as valid STT/dictation language hints (R9-025 reopen
 * — context-pinned dictation): every real locale in {@link LOCALES}, i.e.
 * everything except {@link PSEUDO_LOCALE}. Single source of truth so the
 * "which locales are real languages" policy can't silently drift from
 * {@link LOCALES} as new locales are added.
 */
export const DICTATION_LOCALES: readonly string[] = LOCALES.filter(
  (locale) => locale !== PSEUDO_LOCALE,
);
