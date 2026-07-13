"use client";

import { ExternalLink } from "lucide-react";
import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/button";
import type { ConnectorMeta } from "@/lib/connectors/catalogue";
import type { ConnectorLinkArtifact } from "@/lib/connectors/connect-flow-machine";
import { ExpiryCountdown } from "./expiry-countdown";

/**
 * Spec C6 (T7) — the Discord/Slack OAuth middle step. Authorization requires LEAVING the app:
 * a plain anchor to `authorize_url` is an explicit FULL-PAGE navigation (no popup, no iframe),
 * and the copy says so honestly. The `state` in the URL is the C1 LinkToken (CSRF-covered,
 * C6-D-2); on the platform's redirect back, the connector service 302s to
 * `/settings/connectors?result=…`, handled page-level (`useConnectorReturn`) — the list stays
 * the sole confirmation oracle. The countdown reflects the server state-token expiry.
 */
export function OAuthStep({
  artifact,
  meta,
}: {
  artifact: ConnectorLinkArtifact;
  meta: ConnectorMeta;
}) {
  const t = useTranslations("connectors");
  const platform = t(`platform.${meta.key}`);
  const url = artifact.authorize_url ?? "";

  return (
    <div className="flex flex-col gap-3" data-slot="oauth-step">
      <p className="type-caption text-muted-foreground">
        {t("connect.oauthInstruction", { platform })}
      </p>
      {/* No target=_blank: OAuth is a same-tab, full-page redirect out to the provider and
          back via the connector-service 302 (C6-D-2). rel="noreferrer" trims the referrer. */}
      <a href={url} rel="noreferrer" className="w-fit">
        <Button variant="default" size="sm">
          <ExternalLink className="size-4" aria-hidden />
          {t("connect.continue", { platform })}
        </Button>
      </a>
      {artifact.expires_at ? (
        <ExpiryCountdown expiresAt={artifact.expires_at} />
      ) : null}
    </div>
  );
}
