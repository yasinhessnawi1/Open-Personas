import { getTranslations } from "next-intl/server";
import { ConnectorsManager } from "@/components/connectors/connectors-manager";
import { PageBody, PageHeader } from "@/components/layout";

/**
 * Spec C6 (T4) — the connector management surface (`/settings/connectors`, C6-D-9).
 *
 * A settings subsection listing the six messaging platforms with connected / not-connected
 * state, the connected identity, and (from T5/T9) connect + disconnect. Linking is only ever
 * initiated from this authenticated session (criterion 11). The `intro` line is the light,
 * F1-voice first-connection guidance (C6-D-5) — a note, not a manual.
 */
export default async function ConnectorsPage() {
  const t = await getTranslations("connectors");
  return (
    <PageBody>
      <PageHeader title={t("title")} subtitle={t("subtitle")} />
      <p className="type-caption mb-6 max-w-prose text-muted-foreground">
        {t("intro")}
      </p>
      <ConnectorsManager />
    </PageBody>
  );
}
