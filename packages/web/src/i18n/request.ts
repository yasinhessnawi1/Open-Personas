import { cookies } from "next/headers";
import { getRequestConfig } from "next-intl/server";
import { DEFAULT_LOCALE, LOCALE_COOKIE } from "./config";

// v0.1 ships English only (architecture §2 / spec §2), but every user-facing
// string flows through next-intl's `t()`. Acceptance #9: a stub second locale
// ("xx", a generated pseudo-locale) proves the switch re-renders every string —
// any hard-coded (non-`t()`) string stays plain and stands out. Selected via the
// `NEXT_LOCALE` cookie (set from Settings → Language). No locale-prefixed routing.

const ACCENTS: Record<string, string> = {
  a: "á",
  e: "é",
  i: "í",
  o: "ó",
  u: "ú",
  A: "Á",
  E: "É",
  I: "Í",
  O: "Ó",
  U: "Ú",
};

/**
 * Accent letters outside ICU `{placeholders}` and rich-text `<tags>` so both
 * interpolation and `t.rich()` still resolve. Accenting a tag name would turn
 * `<em>` into an accented tag name, which `t.rich` cannot match to its handler.
 */
function pseudoString(value: string): string {
  const accented = value
    .split(/(\{[^}]*\}|<\/?[a-zA-Z][a-zA-Z0-9]*>)/) // keep {name} + <tag> intact
    .map((part) =>
      part.startsWith("{") || part.startsWith("<")
        ? part
        : part.replace(/[aeiouAEIOU]/g, (c) => ACCENTS[c] ?? c),
    )
    .join("");
  return `«${accented}»`;
}

type Messages = { [key: string]: string | Messages };

function pseudoMessages(messages: Messages): Messages {
  const out: Messages = {};
  for (const [key, value] of Object.entries(messages)) {
    out[key] =
      typeof value === "string" ? pseudoString(value) : pseudoMessages(value);
  }
  return out;
}

export default getRequestConfig(async () => {
  const store = await cookies();
  const requested = store.get(LOCALE_COOKIE)?.value;
  const locale = requested === "xx" ? "xx" : DEFAULT_LOCALE;
  const en = (await import("./messages/en.json")).default as Messages;
  return {
    locale,
    messages: locale === "xx" ? pseudoMessages(en) : en,
  };
});
