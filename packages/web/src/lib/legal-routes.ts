/**
 * Where the legal documents live (D-OWNER-11-18 (4)).
 *
 * One home for the two paths so the sign-up line, the account menu and the legal page
 * footer cannot drift apart. The pages are `src/app/legal/privacy/page.tsx` and
 * `src/app/legal/terms/page.tsx`; a test pins that every path here has a page.
 */
export const LEGAL_ROUTES = {
  privacy: "/legal/privacy",
  terms: "/legal/terms",
} as const;

export type LegalDocumentKey = keyof typeof LEGAL_ROUTES;
