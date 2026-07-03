"use client";

import { ExternalLink } from "lucide-react";
import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/button";
import type { ConnectorMeta } from "@/lib/connectors/catalogue";
import type { ConnectorLinkArtifact } from "@/lib/connectors/connect-flow-machine";
import { CopyButton } from "./copy-button";
import { ExpiryCountdown } from "./expiry-countdown";

/**
 * Spec C6 (T6) — the Telegram deep-link middle step. The user opens the bot via the `t.me`
 * deep link (its `?start=<token>` binds server-side, C2); the web only presents it and then
 * awaits the binding (the ConnectFlow poll). A copy-link affordance covers the desktop→phone
 * case (no QR dependency in v1 — deferred), and the contextual line (C6-D-5) says exactly
 * what to do in the app. The live countdown reflects the server `expires_at` (C6-D-8).
 */
export function DeepLinkStep({
  artifact,
  meta,
}: {
  artifact: ConnectorLinkArtifact;
  meta: ConnectorMeta;
}) {
  const t = useTranslations("connectors");
  const link = artifact.deep_link ?? "";
  const platform = t(`platform.${meta.key}`);

  return (
    <div className="flex flex-col gap-3" data-slot="deep-link-step">
      <p className="type-caption text-muted-foreground">
        {t("connect.telegramInstruction", { platform })}
      </p>
      <div className="flex flex-wrap items-center gap-2">
        {/* Opens in a NEW tab (never navigates this page away): the SPA must stay alive so
            the ConnectFlow poll can land the binding while the user presses Start in Telegram
            (T6 carry-item). noopener/noreferrer for the standard new-tab hardening. */}
        <a
          href={link}
          target="_blank"
          rel="noopener noreferrer"
          className="w-fit"
        >
          <Button variant="default" size="sm">
            <ExternalLink className="size-4" aria-hidden />
            {t("connect.open", { platform })}
          </Button>
        </a>
        <CopyButton value={link} label={t("connect.copyLink")} />
      </div>
      {artifact.expires_at ? (
        <ExpiryCountdown expiresAt={artifact.expires_at} />
      ) : null}
    </div>
  );
}
