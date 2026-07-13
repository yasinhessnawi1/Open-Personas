"use client";

import { Check, Clock, KeyRound, Link2, MessageSquareCode } from "lucide-react";
import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import type { ConnectorMeta } from "@/lib/connectors/catalogue";
import { formatIdentity } from "@/lib/connectors/format";
import type { ConnectorConnection } from "@/lib/connectors/use-connectors";
import { cn } from "@/lib/utils";
import { ConnectorBrandIcon } from "./connector-brand-icon";

export interface ConnectorCardProps {
  meta: ConnectorMeta;
  /** The active binding for this platform, or `null` when not connected. */
  connection: ConnectorConnection | null;
  /** First-load flag — shows a neutral "checking" state rather than a false "not connected". */
  loading?: boolean;
  /** Disables the action while a connect/disconnect is in flight. */
  busy?: boolean;
  onConnect?: (meta: ConnectorMeta) => void;
  onDisconnect?: (connection: ConnectorConnection) => void;
}

/** The connect mechanism, shown as a small honest line (kit `.mech`). */
const MECHANISM_ICON = {
  deep_link: Link2,
  code: MessageSquareCode,
  oauth: KeyRound,
} as const;

/**
 * Spec C6 (T4) → R11-B4 — one platform card in the kit register: the REAL brand
 * tile (owner-ruled), name + state pill (Connected green / Coming soon amber —
 * an UNWIRED backend reads as coming soon, never as a Connect button that can
 * only 503, the wired-capability rule T10), the connected identity or the
 * platform blurb + connect-mechanism line, and the action. Pure presentation;
 * connect (T5) / disconnect (T9) are injected.
 */
export function ConnectorCard({
  meta,
  connection,
  loading = false,
  busy = false,
  onConnect,
  onDisconnect,
}: ConnectorCardProps) {
  const t = useTranslations("connectors");
  const connected = connection !== null;
  const comingSoon = !meta.backendReady && !connected;
  const MechIcon = MECHANISM_ICON[meta.mechanism];

  return (
    <Card
      className={cn(
        "flex flex-row items-center gap-3.5 rounded-xl p-4",
        comingSoon && "opacity-65",
      )}
      data-slot="connector-card"
      data-platform={meta.key}
      data-connected={connected}
    >
      <ConnectorBrandIcon platform={meta.key} />

      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <p className="type-body font-medium">{t(`platform.${meta.key}`)}</p>
          {loading && !connected ? (
            <span className="rounded-full border border-border px-2 py-0.5 text-[11px] text-muted-foreground">
              {t("state.checking")}
            </span>
          ) : connected ? (
            <span className="flex items-center gap-1 rounded-full border border-emerald-500/40 px-2 py-0.5 text-[11px] font-medium text-emerald-700 dark:text-emerald-400">
              <Check className="size-3" aria-hidden="true" />
              {t("state.connected")}
            </span>
          ) : comingSoon ? (
            <span className="rounded-full border border-amber-500/45 px-2 py-0.5 text-[11px] font-medium text-amber-600 dark:text-amber-500">
              {t("state.comingSoon")}
            </span>
          ) : null}
        </div>
        {connected ? (
          <p className="type-caption flex items-center gap-1.5 truncate text-muted-foreground">
            <span
              aria-hidden="true"
              className="size-1.5 shrink-0 rounded-full bg-emerald-500"
            />
            {formatIdentity(meta.identityKind, connection.platform_identity, t)}
            {" · "}
            {t("linkedOn", {
              date: new Date(connection.linked_at).toLocaleDateString(),
            })}
          </p>
        ) : (
          <>
            <p className="type-caption truncate text-muted-foreground">
              {t(`desc.${meta.key}`)}
            </p>
            {!comingSoon ? (
              <p className="type-caption mt-0.5 flex items-center gap-1 text-muted-foreground/80">
                <MechIcon className="size-3" aria-hidden="true" />
                {t(`mechanism.${meta.mechanism}`)}
              </p>
            ) : null}
          </>
        )}
      </div>

      {connected ? (
        <Button
          variant="outline"
          size="sm"
          disabled={busy}
          onClick={() => onDisconnect?.(connection)}
        >
          {t("action.disconnect")}
        </Button>
      ) : comingSoon ? (
        <Clock
          className="size-4 shrink-0 text-muted-foreground"
          aria-label={t("state.comingSoon")}
        />
      ) : (
        <Button
          variant="default"
          size="sm"
          disabled={busy || loading}
          onClick={() => onConnect?.(meta)}
        >
          {t("action.connect")}
        </Button>
      )}
    </Card>
  );
}
