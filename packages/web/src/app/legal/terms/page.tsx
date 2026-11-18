import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";
import { LegalDocumentView } from "@/components/legal/legal-document";
import { TERMS_OF_SERVICE } from "@/content/legal/terms";

/**
 * The Terms of Service (D-OWNER-11-18 (4)). Public, like `/account-deleted`: a visitor
 * reads it before they have an account. The text lives in `src/content/legal/terms.ts`.
 */
export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("legal");
  return { title: t("termsTitle") };
}

export default function TermsPage() {
  return <LegalDocumentView doc={TERMS_OF_SERVICE} />;
}
