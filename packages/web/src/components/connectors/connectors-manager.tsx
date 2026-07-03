"use client";

import { useTranslations } from "next-intl";
import { useMemo, useState } from "react";
import { Stack } from "@/components/layout";
import { Button } from "@/components/ui/button";
import {
  CONNECTOR_CATALOGUE,
  type ConnectorMeta,
} from "@/lib/connectors/catalogue";
import { useConnectorReturn } from "@/lib/connectors/use-connector-return";
import {
  type ConnectorConnection,
  useConnectors,
} from "@/lib/connectors/use-connectors";
import { useDisconnect } from "@/lib/connectors/use-disconnect";
import { ConnectFlow } from "./connect-flow";
import { ConnectorCard } from "./connector-card";

/**
 * Spec C6 (T4) — the connectors surface: the six platforms with live connected /
 * not-connected state + the connected identity, merged from `GET /v1/me/connectors`.
 *
 * T4 is the read surface + states; the connect flow (T5) and disconnect (T9) attach their
 * `onConnect` / `onDisconnect` handlers to the cards in those tasks. The list this renders
 * is also the flows' completion oracle (C6-D-1): `refresh()` re-syncs after a connect/
 * disconnect so state is the source of truth, never an optimistic chip.
 */
export function ConnectorsManager() {
  const t = useTranslations("connectors");
  const { connections, loading, error, refresh } = useConnectors();
  // Handle the OAuth 302-return (T7): refresh the list (sole oracle) + an honest ephemeral
  // toast + strip ?result from the URL. Page-level, zero modal state — works on a cold load.
  useConnectorReturn(refresh);
  const disconnect = useDisconnect(refresh);
  // The platform whose ConnectFlow is open (T5). Mounted per-open so the machine resets each
  // time; `null` closes it.
  const [connecting, setConnecting] = useState<ConnectorMeta | null>(null);

  // First active binding per platform (the list is newest-first). v1 shows one connection
  // per platform even though the data model permits several.
  const byPlatform = useMemo(() => {
    const map = new Map<string, ConnectorConnection>();
    for (const c of connections) {
      if (!map.has(c.platform)) map.set(c.platform, c);
    }
    return map;
  }, [connections]);

  const initialLoading = loading && connections.length === 0 && error === null;
  const noneConnected = !loading && error === null && connections.length === 0;

  // First-connection guidance (C6-D-5) invites ONLY the platforms whose backend is wired
  // today (the wired-capability rule, T10) — so a first-timer isn't pointed at Discord/Slack
  // while their OAuth issue routes are unmounted (C6-KL-2). Natural-language "A, B, or C".
  const readyList = useMemo(() => {
    const names = CONNECTOR_CATALOGUE.filter((m) => m.backendReady).map((m) =>
      t(`platform.${m.key}`),
    );
    if (names.length <= 1) return names.join("");
    return `${names.slice(0, -1).join(", ")}, ${t("firstConnection.or")} ${names[names.length - 1]}`;
  }, [t]);

  return (
    <Stack gap={4} data-slot="connectors-manager">
      {error ? (
        <div
          role="alert"
          data-slot="connectors-error"
          className="flex items-center justify-between gap-3 rounded-lg border border-destructive/30 bg-destructive/5 px-4 py-3"
        >
          <p className="type-ui text-destructive">{t("error.load")}</p>
          <Button variant="outline" size="sm" onClick={() => void refresh()}>
            {t("error.retry")}
          </Button>
        </div>
      ) : null}

      {noneConnected ? (
        <div
          data-slot="first-connection-guidance"
          className="rounded-lg border bg-muted/30 px-4 py-3"
        >
          <p className="type-ui font-medium">{t("firstConnection.title")}</p>
          <p className="type-caption text-muted-foreground">
            {t("firstConnection.body", { platforms: readyList })}
          </p>
        </div>
      ) : null}

      <Stack gap={3}>
        {CONNECTOR_CATALOGUE.map((meta) => (
          <ConnectorCard
            key={meta.key}
            meta={meta}
            connection={byPlatform.get(meta.key) ?? null}
            loading={initialLoading}
            onConnect={setConnecting}
            onDisconnect={(connection) => void disconnect(meta, connection)}
          />
        ))}
      </Stack>

      {connecting ? (
        <ConnectFlow
          meta={connecting}
          connected={byPlatform.has(connecting.key)}
          refresh={refresh}
          onClose={() => setConnecting(null)}
        />
      ) : null}
    </Stack>
  );
}
