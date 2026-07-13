import { getTranslations } from "next-intl/server";

import { ActivityTabs } from "@/components/activity/activity-tabs";
import { AutonomyControls } from "@/components/activity/autonomy-controls";
import { Review } from "@/components/activity/review";
import { Onboarding } from "@/components/home/onboarding";
import { PageBody, PageHeader } from "@/components/layout";
import { Separator } from "@/components/ui/separator";
import { unwrap } from "@/lib/api";
import { serverApi } from "@/lib/api/server";

/**
 * Spec A6 (W5) — the morning Review: "what did my personas do while I slept?" (criterion 11).
 *
 * The Activity area's landing (A6-D-1). Renders the ONE shared MorningDigest (B5) in the ratified
 * Dateline-spine + Ledger-affordances direction: editorial calm by default, waiting/stuck carry a
 * pill + inline action, done collapses to one-liners, and stuck is the ONE loud red-rail card. Read
 * in under a minute (A6-R-1), waiting-first (A6-D-2), loud only where it informs — the five-test
 * calm rubric. The digest is fetched client-side (the A6 endpoint regenerates into the typed client
 * at merge-back — the W4/A8 precedent); opening it consumes the deferred chatter once, server-side.
 *
 * R11-B1 (D-R11-1/2/4): this is now the app's landing surface — `/` redirects signed-in users
 * here and the old `/review` route redirects in. The Home dashboard retired with the move, so the
 * brand-new-user empty state re-homes here: a CONFIRMED zero personas renders the onboarding
 * instead of an empty review (a failed personas fetch keeps the review — never show "create your
 * first persona" to a user whose personas merely failed to load).
 */
export default async function ActivityPage() {
  const t = await getTranslations("review");
  const api = await serverApi();
  let personasKnown = true;
  const personas = await unwrap(await api.GET("/v1/personas")).catch(() => {
    personasKnown = false;
    return [];
  });
  if (personasKnown && personas.length === 0) {
    const th = await getTranslations("home");
    return (
      <PageBody>
        <PageHeader title={th("title")} subtitle={th("subtitleNew")} />
        <Onboarding />
      </PageBody>
    );
  }
  const personaList = personas.map((p) => ({ id: p.id, name: p.name }));
  return (
    <PageBody width="narrow">
      <ActivityTabs />
      <PageHeader title={t("title")} subtitle={t("subtitle")} />
      <Review />
      <Separator className="my-8" />
      <AutonomyControls personas={personaList} />
    </PageBody>
  );
}
