"use client";

import { useTranslations } from "next-intl";
import type { ConnectorMeta } from "@/lib/connectors/catalogue";
import type { ConnectorLinkArtifact } from "@/lib/connectors/connect-flow-machine";
import { CopyButton } from "./copy-button";
import { ExpiryCountdown } from "./expiry-countdown";

/**
 * Spec C6 (T8) — the WhatsApp/SMS/Email code step (the REVERSED flow, D-C4-5 / D-C5-X): the
 * web shows the issued code + the PUBLIC destination, and the user sends the code FROM the
 * account to bind (the signature/DMARC-verified envelope binds server-side). The user needs
 * BOTH halves, so the code and the destination each get a copy affordance and the instruction
 * names the physical action ("text"/"email this code to …"). The code is shown with visual
 * letter-spacing only (CSS) — copy always yields the canonical string the server matches
 * (bar 4). Confirmation is the list poll (T5), unchanged; the countdown is the server expiry.
 */
export function CodeStep({
  artifact,
  meta,
}: {
  artifact: ConnectorLinkArtifact;
  meta: ConnectorMeta;
}) {
  const t = useTranslations("connectors");
  const platform = t(`platform.${meta.key}`);
  const code = artifact.code ?? "";
  const destination = artifact.destination ?? "";
  const isEmail = meta.identityKind === "email";

  const instruction = destination
    ? isEmail
      ? t("connect.emailInstruction", { destination })
      : t("connect.textInstruction", { destination })
    : t("connect.codeInstructionNoDest", { platform });

  return (
    <div className="flex flex-col gap-3" data-slot="code-step">
      <div className="flex flex-col gap-1">
        <span className="type-caption text-muted-foreground">
          {t("connect.code")}
        </span>
        <div className="flex flex-wrap items-center gap-2">
          {/* Visual spacing only — the text content stays the canonical code; copy uses it verbatim. */}
          <code
            className="type-body rounded-md bg-muted px-3 py-1.5 font-mono text-base tracking-[0.3em]"
            data-slot="connect-code"
          >
            {code}
          </code>
          <CopyButton value={code} label={t("connect.copyCode")} />
        </div>
      </div>

      <p className="type-caption text-muted-foreground">{instruction}</p>

      {destination ? (
        <div className="flex flex-wrap items-center gap-2">
          <span
            className="type-caption font-medium"
            data-slot="connect-destination"
          >
            {destination}
          </span>
          <CopyButton
            value={destination}
            label={t("connect.copyDestination")}
          />
        </div>
      ) : null}

      {artifact.expires_at ? (
        <ExpiryCountdown expiresAt={artifact.expires_at} />
      ) : null}
    </div>
  );
}
