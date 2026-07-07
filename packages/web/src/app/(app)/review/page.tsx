import { getTranslations } from "next-intl/server";

import { ActivityTabs } from "@/components/activity/activity-tabs";
import { Review } from "@/components/activity/review";
import { PageBody, PageHeader } from "@/components/layout";

/**
 * Spec A6 (W5) — the morning Review: "what did my personas do while I slept?" (criterion 11).
 *
 * The Activity area's landing (A6-D-1). Renders the ONE shared MorningDigest (B5) in the ratified
 * Dateline-spine + Ledger-affordances direction: editorial calm by default, waiting/stuck carry a
 * pill + inline action, done collapses to one-liners, and stuck is the ONE loud red-rail card. Read
 * in under a minute (A6-R-1), waiting-first (A6-D-2), loud only where it informs — the five-test
 * calm rubric. The digest is fetched client-side (the A6 endpoint regenerates into the typed client
 * at merge-back — the W4/A8 precedent); opening it consumes the deferred chatter once, server-side.
 */
export default async function ReviewPage() {
  const t = await getTranslations("review");
  return (
    <PageBody width="narrow">
      <ActivityTabs />
      <PageHeader title={t("title")} subtitle={t("subtitle")} />
      <Review />
    </PageBody>
  );
}
