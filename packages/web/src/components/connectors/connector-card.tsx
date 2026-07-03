"use client";

import { Check } from "lucide-react";
import { useTranslations } from "next-intl";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import type { ConnectorMeta } from "@/lib/connectors/catalogue";
import { formatIdentity } from "@/lib/connectors/format";
import type { ConnectorConnection } from "@/lib/connectors/use-connectors";

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

/**
 * Spec C6 (T4) — one platform row in the connectors surface: icon + name + state badge +
 * the connected identity (or a not-connected blurb) + the connect/disconnect action. Pure
 * presentation (F2 components only); the connect flow (T5) and disconnect (T9) are injected
 * via `onConnect` / `onDisconnect`.
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
  const Icon = meta.icon;
  const connected = connection !== null;

  return (
    <Card
      className="flex flex-row items-center gap-4 p-4 sm:p-5"
      data-slot="connector-card"
      data-platform={meta.key}
      data-connected={connected}
    >
      <span
        className="grid size-10 shrink-0 place-items-center rounded-lg bg-muted text-muted-foreground"
        aria-hidden
      >
        <Icon className="size-5" />
      </span>

      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <p className="type-body font-medium">{t(`platform.${meta.key}`)}</p>
          {loading && !connected ? (
            <Badge variant="ghost">{t("state.checking")}</Badge>
          ) : connected ? (
            <Badge variant="secondary" className="gap-1">
              <Check className="size-3" aria-hidden />
              {t("state.connected")}
            </Badge>
          ) : (
            <Badge variant="outline">{t("state.notConnected")}</Badge>
          )}
        </div>
        {connected ? (
          <p className="type-caption truncate text-muted-foreground">
            {formatIdentity(meta.identityKind, connection.platform_identity, t)}
            {" · "}
            {t("linkedOn", {
              date: new Date(connection.linked_at).toLocaleDateString(),
            })}
          </p>
        ) : (
          <p className="type-caption text-muted-foreground">
            {t("notConnectedHint")}
          </p>
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
