import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";
import { LegalDocumentView } from "@/components/legal/legal-document";
import { PRIVACY_POLICY } from "@/content/legal/privacy";

/**
 * The Privacy Policy (D-OWNER-11-18 (4)). Public, like `/account-deleted`: a visitor
 * reads it before they have an account. The text lives in `src/content/legal/privacy.ts`.
 */
export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("legal");
  return { title: t("privacyTitle") };
}

export default function PrivacyPage() {
  return <LegalDocumentView doc={PRIVACY_POLICY} />;
}
