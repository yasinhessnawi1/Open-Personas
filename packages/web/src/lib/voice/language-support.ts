/**
 * Spec 32 D — author-time voice-language hint (client mirror of the backend
 * capability registry). Warns in the persona form when a declared language the
 * configured voice providers can't serve is entered, so the author learns before
 * a call (the call-time soft-fallback still keeps the call from crashing).
 *
 * Hand-mirrored from the served set + normalization in:
 *   packages/core/src/persona/language_capability.py (CanonicalLanguage + _ALIASES)
 * Keep in sync — the registry is the source of truth.
 */

const SERVED = new Set(["en", "no", "de", "fr", "es", "ar"]);

const ALIASES: Record<string, string> = {
  eng: "en",
  nb: "no",
  nn: "no",
  nob: "no",
  nno: "no",
  nor: "no",
  ger: "de",
  deu: "de",
  fra: "fr",
  fre: "fr",
  spa: "es",
  ara: "ar",
};

function normalize(raw: string): string | null {
  const key = raw.trim().toLowerCase();
  if (key === "") return null;
  if (SERVED.has(key)) return key;
  if (key in ALIASES) return ALIASES[key];
  const base = key.split("-")[0];
  if (SERVED.has(base)) return base;
  if (base in ALIASES) return ALIASES[base];
  return null;
}

/**
 * The declared language, when the voice providers can't serve it; null when
 * they can (or while the field is blank, so it doesn't nag mid-typing). The
 * caller renders `author.voiceLanguageWarning` around it, so the sentence
 * lives in the message catalogue rather than here.
 */
export function unservedVoiceLanguage(languageDefault: string): string | null {
  const trimmed = languageDefault.trim();
  if (trimmed === "") return null;
  if (normalize(trimmed) !== null) return null;
  return trimmed;
}
