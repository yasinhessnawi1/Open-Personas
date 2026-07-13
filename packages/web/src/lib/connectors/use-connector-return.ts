"use client";

import { useTranslations } from "next-intl";
import { useEffect, useRef } from "react";
import { useToast } from "@/components/patterns/toast";
import { resolveReturnToast } from "./connect-return";
import type { ConnectorConnection } from "./use-connectors";

/**
 * Spec C6 (T7) — handle the OAuth 302-return to the connectors surface, `?result=…` (C6-D-2).
 * R11-B1: the surface lives at `/connectors`; provider-registered return URIs may still
 * point at the old `/settings/connectors`, which redirects here with the query intact.
 *
 * Page-level, zero modal state (the full-page OAuth round-trip discards the ConnectFlow) — it
 * works on a cold load. On return it (1) refreshes the list — the SOLE confirmation oracle,
 * (2) shows an ephemeral toast whose success is contingent on the *refreshed list* (a forged
 * `result=connected` with no binding → an honest "unconfirmed", never success), and (3) strips
 * `?result` (+ `platform`) via `replaceState` so a refresh / back-nav never re-toasts. Consumed
 * exactly once (a ref guard, so React's double-invoke and re-renders can't re-fire it).
 *
 * Reads `window.location.search` (not `useSearchParams`) deliberately: this is a client-only
 * effect, so it needs no Suspense boundary and stays a pure side-effect on mount.
 */
export function useConnectorReturn(
  refresh: () => Promise<ConnectorConnection[]>,
): void {
  const t = useTranslations("connectors");
  const toast = useToast();
  const handled = useRef(false);

  useEffect(() => {
    if (handled.current) return;
    const params = new URLSearchParams(window.location.search);
    const result = params.get("result");
    if (!result) return;
    handled.current = true;
    const platform = params.get("platform");

    void (async () => {
      const fresh = await refresh();
      const connected =
        platform !== null && fresh.some((c) => c.platform === platform);
      switch (resolveReturnToast(result, connected)) {
        case "success":
          toast.success(t("return.success"));
          break;
        case "unconfirmed":
          toast.warning(t("return.unconfirmed"));
          break;
        case "denied":
          toast.info(t("return.denied"));
          break;
        case "expired":
          toast.error(t("return.expired"));
          break;
        case "failed":
          toast.error(t("return.failed"));
          break;
        default:
          break;
      }
    })();

    // Clean the URL immediately so a reload / back never re-triggers (single-consume).
    window.history.replaceState(null, "", window.location.pathname);
  }, [refresh, t, toast]);
}
