"use client";

import { useTranslations } from "next-intl";
import { useCallback } from "react";
import { useToast } from "@/components/patterns/toast";
import { useConfirm } from "@/components/providers/confirm-provider";
import { unwrap } from "@/lib/api/client";
import { useApi } from "@/lib/api/use-api";
import type { ConnectorMeta } from "./catalogue";
import { formatIdentity } from "./format";
import type { ConnectorConnection } from "./use-connectors";

/**
 * Spec C6 (T9) — disconnect a platform (criterion 9). The user first SEES what they're
 * severing (a danger-toned confirm naming the platform + the exact connected identity + the
 * concrete consequence — the see-then-grant grammar's inverse), then one tap drives C1's real
 * unlink via `DELETE /v1/me/connectors/{platform}/{identity}`.
 *
 * Sole-oracle discipline applies to disconnect too (C6-D-1): the severed state is NOT applied
 * optimistically — it comes from the `refresh()` refetch after a 204. On failure the card is
 * left untouched and the error surfaces in the honest voice (no phantom-severed UI).
 */
export function useDisconnect(refresh: () => Promise<unknown>) {
  const t = useTranslations("connectors");
  const api = useApi();
  const confirm = useConfirm();
  const toast = useToast();

  return useCallback(
    async (
      meta: ConnectorMeta,
      connection: ConnectorConnection,
    ): Promise<void> => {
      const platform = t(`platform.${meta.key}`);
      const identity = formatIdentity(
        meta.identityKind,
        connection.platform_identity,
        t,
      );

      const ok = await confirm({
        title: t("disconnect.title", { platform }),
        description: t("disconnect.description", { platform, identity }),
        confirmLabel: t("action.disconnect"),
        tone: "danger",
      });
      if (!ok) return;

      try {
        await unwrap(
          await api.DELETE("/v1/me/connectors/{platform}/{platform_identity}", {
            params: {
              path: {
                platform: meta.key,
                platform_identity: connection.platform_identity,
              },
            },
          }),
        );
        // The list is the sole oracle: the card flips to not-connected from the refetch,
        // never an optimistic removal the backend hasn't confirmed.
        await refresh();
        toast.success(t("disconnect.done", { platform }));
      } catch {
        // Honest voice; the card is unchanged (the binding is still active), no phantom sever.
        toast.error(t("disconnect.failed", { platform }));
      }
    },
    [t, api, confirm, toast, refresh],
  );
}
