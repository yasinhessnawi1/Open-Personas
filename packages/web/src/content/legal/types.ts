/**
 * The shape of a legal document (Privacy Policy, Terms of Service).
 *
 * The text lives in one content module per document (`privacy.ts`, `terms.ts`) as
 * Markdown, so a change to the product is a change to one readable file. The page only
 * renders it. `pnpm check:copy` gates these modules for dashes and banned vocabulary,
 * the same way it gates the message catalogue. See MAINTAINING.md next to this file for
 * which product facts each section depends on.
 */
export interface LegalSection {
  /** Stable anchor id, used as the heading's `id` so a section can be linked to. */
  readonly id: string;
  readonly heading: string;
  /** Markdown body. No raw HTML (the renderer does not allow it). */
  readonly body: string;
}

export interface LegalDocument {
  readonly title: string;
  /** Semantic version of the text. Bump on every change a user should know about. */
  readonly version: string;
  /** ISO date (YYYY-MM-DD) of the last change. */
  readonly lastUpdated: string;
  /** Markdown shown under the title, before the sections. */
  readonly intro: string;
  readonly sections: readonly LegalSection[];
}
