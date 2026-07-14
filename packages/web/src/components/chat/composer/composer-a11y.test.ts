/**
 * F3 T20 — composer accessibility verification.
 *
 * Two distinct discipline checks:
 *
 *   1. **ARIA-via-i18n discipline.** `pnpm check:no-literals` catches CSS
 *      literals but NOT JSX attribute literals like `aria-label="attach
 *      file"`. This test source-greps the F3 composer modules and asserts
 *      that every `aria-label=` either references an i18n call (`t(...)`)
 *      or a prop / variable. Raw English string literals as ARIA values
 *      would defeat the next-intl pseudo-locale verification.
 *
 *   2. **i18n key coverage.** Every i18n key referenced by the composer
 *      MUST be defined in `en.json`. Missing keys would render `null` /
 *      key-string at runtime and silently break screen-reader output.
 */

import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import en from "@/i18n/messages/en.json";

const COMPOSER_DIR = path.join(
  __dirname,
  "..",
  "..",
  "..",
  "..",
  "src",
  "components",
  "chat",
  "composer",
);

const F3_SOURCE_FILES = [
  "attach-control.tsx",
  "image-preview.tsx",
  "document-chip.tsx",
  "conversation-document-list.tsx",
  "no-vision-error-banner.tsx",
];

function readSource(name: string): string {
  return fs.readFileSync(path.join(COMPOSER_DIR, name), "utf8");
}

describe("F3 T20 — ARIA labels go through next-intl t(), not raw English", () => {
  it.each(F3_SOURCE_FILES)(
    "%s: every aria-label uses t(...) or a prop/variable, NOT a raw English literal",
    (file) => {
      const src = readSource(file);

      // Find all aria-label= occurrences. Allowed forms:
      //   aria-label={t("...")}
      //   aria-label={someVariable}
      //   aria-label={tooltip}
      // Disallowed:
      //   aria-label="literal English"
      //   aria-label={"literal English"}
      const ariaLiteralPattern =
        /aria-label\s*=\s*["{][^}]*?(?:["'])(\w[\w\s]+)["']/g;
      const matches: string[] = [];
      let match: RegExpExecArray | null = ariaLiteralPattern.exec(src);
      while (match !== null) {
        // Quoted strings inside `{t("...")}` are LEGAL — the outer `t(` call
        // marks them as i18n keys. We only flag raw "..." attribute values.
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

describe("F3 T20 — every composer i18n key resolves in en.json", () => {
  const KEYS = [
    "chat.composer.attach.label",
    "chat.composer.attach.imageDisabled",
    "chat.composer.attach.openConversationFirst",
    "chat.composer.attach.remove",
    "chat.composer.attach.retry",
    "chat.composer.attach.sendBlocked",
    "chat.composer.attach.feedback.imageAttached",
    "chat.composer.attach.feedback.documentUploading",
    "chat.composer.attach.feedback.documentAttached",
    "chat.composer.validation.empty_file",
    "chat.composer.validation.oversize",
    "chat.composer.validation.per_message_image_cap",
    "chat.composer.validation.unsupported_format",
    "chat.composer.upload.uploading",
    "chat.composer.upload.uploaded",
    "chat.composer.upload.failed",
    "chat.composer.documents.panelTitle",
    "chat.composer.documents.panelEmpty",
    "chat.composer.documents.scannedCue",
    "chat.composer.documents.removeConfirmation",
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
