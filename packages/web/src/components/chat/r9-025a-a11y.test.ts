/**
 * R9-025a — message-action-bar + mic-dictation accessibility verification.
 *
 * Mirrors `composer/composer-a11y.test.ts`'s two-discipline pattern (F3 T20),
 * extended to the R9-025a surface (action bar + shared mic-dictation, mounted
 * in both the chat composer and the persona-authoring wizard):
 *
 *   1. **ARIA-via-i18n discipline.** `pnpm check:no-literals` catches CSS
 *      literals but NOT JSX attribute literals like `aria-label="copy"`.
 *      Source-greps the R9-025a modules and asserts every `aria-label=`
 *      either references an i18n call (`t(...)`) or a prop/variable.
 *   2. **i18n key coverage.** Every i18n key the R9-025a surface references
 *      MUST be defined in `en.json`.
 */

import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import en from "@/i18n/messages/en.json";

const SOURCE_FILES = ["message-action-bar.tsx", "mic-dictation.tsx"];

function readSource(name: string): string {
  return fs.readFileSync(path.join(__dirname, name), "utf8");
}

describe("R9-025a — ARIA labels go through next-intl t(), not raw English", () => {
  it.each(SOURCE_FILES)(
    "%s: every aria-label uses t(...) or a prop/variable, NOT a raw English literal",
    (file) => {
      const src = readSource(file);

      // Same pattern as composer-a11y.test.ts: flag `aria-label="literal"`
      // (bare double-quoted JSX attribute) while allowing `aria-label={t(...)}`
      // / `aria-label={someVariable}`.
      const ariaLiteralPattern =
        /aria-label\s*=\s*["{][^}]*?(?:["'])(\w[\w\s]+)["']/g;
      const matches: string[] = [];
      let match: RegExpExecArray | null = ariaLiteralPattern.exec(src);
      while (match !== null) {
        const fullMatch = match[0];
        if (fullMatch.includes("t(") || fullMatch.includes("t.rich(")) {
          match = ariaLiteralPattern.exec(src);
          continue;
        }
        if (fullMatch.startsWith('aria-label="')) {
          matches.push(fullMatch);
        }
        match = ariaLiteralPattern.exec(src);
      }

      expect(matches).toEqual([]);
    },
  );
});

describe("R9-025a — every referenced i18n key resolves in en.json", () => {
  const KEYS = [
    "chat.actions.copy",
    "chat.actions.copied",
    "chat.actions.retry",
    "chat.actions.readAloud",
    "chat.actions.stopReading",
    "chat.actions.loadingAudio",
    "mic.start",
    "mic.stop",
    "mic.transcribing",
    "mic.permissionDenied",
  ];

  it.each(KEYS)("%s is defined", (key) => {
    const parts = key.split(".");
    let cursor: unknown = en;
    for (const part of parts) {
      if (typeof cursor !== "object" || cursor === null) {
        throw new Error(`key ${key}: ${part} missing — cursor not an object`);
      }
      cursor = (cursor as Record<string, unknown>)[part];
      if (cursor === undefined) {
        throw new Error(`key ${key}: ${part} not in en.json`);
      }
    }
    expect(typeof cursor).toBe("string");
  });
});
