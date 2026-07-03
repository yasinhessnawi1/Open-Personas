"use client";

import { Dialog } from "@base-ui/react/dialog";
import { Check, Loader2 } from "lucide-react";
import { useTranslations } from "next-intl";
import { useCallback, useEffect } from "react";
import { Button } from "@/components/ui/button";
import { unwrap } from "@/lib/api/client";
import { useApi } from "@/lib/api/use-api";
import type { ConnectorMeta } from "@/lib/connectors/catalogue";
import type { ConnectorLinkArtifact } from "@/lib/connectors/connect-flow-machine";
import { useConnectFlow } from "@/lib/connectors/use-connect-flow";
import { PlatformStep } from "./steps/platform-step";

export interface ConnectFlowProps {
  meta: ConnectorMeta;
  /** Whether this platform is currently connected (from the list — the completion oracle). */
  connected: boolean;
  /** Re-fetch the connectors list (the ONLY success signal, C6-D-1). Result unused here. */
  refresh: () => Promise<unknown>;
  /** Called when the dialog is dismissed (the manager unmounts the flow). */
  onClose: () => void;
}

/**
 * Spec C6 (T5) — the ConnectFlow dialog: the one coherent frame over four mechanisms
 * (C6-D-1). `initiating → awaiting → confirmed`, with honest `failed` / `expired` branches.
 * The middle step is generic here (open a link / continue / show the code); T6–T8 refine
 * each mechanism's presentation (countdown, resend, OAuth redirect). Auto-initiates on open;
 * closing stops the poll (the hook clears its interval on unmount).
 */
export function ConnectFlow({
  meta,
  connected,
  refresh,
  onClose,
}: ConnectFlowProps) {
  const t = useTranslations("connectors");
  const api = useApi();

  const initiateLink = useCallback(
    async (): Promise<ConnectorLinkArtifact> =>
      unwrap(
        await api.POST("/v1/me/connectors/{platform}/link", {
          params: { path: { platform: meta.key } },
        }),
      ),
    [api, meta.key],
  );

  const { state, initiate, reissue, close } = useConnectFlow({
    connected,
    initiateLink,
    refresh,
  });

  // Auto-initiate once when the dialog opens (the flow is mounted per-open by the manager).
  useEffect(() => {
    initiate();
  }, [initiate]);

  const handleOpenChange = (open: boolean) => {
    if (!open) {
      close();
      onClose();
    }
  };

  const platform = t(`platform.${meta.key}`);

  return (
    <Dialog.Root open onOpenChange={handleOpenChange}>
      <Dialog.Portal>
        <Dialog.Backdrop className="fixed inset-0 z-50 bg-black/40 transition-opacity duration-[var(--motion-duration-fast)] data-ending-style:opacity-0 data-starting-style:opacity-0 supports-backdrop-filter:backdrop-blur-xs" />
        <Dialog.Popup
          data-slot="connect-flow"
          data-platform={meta.key}
          data-status={state.status}
          className="-translate-x-1/2 -translate-y-1/2 fixed top-1/2 left-1/2 z-50 flex w-[min(28rem,calc(100vw-2rem))] flex-col gap-3 rounded-xl border bg-popover bg-clip-padding p-5 text-popover-foreground shadow-[var(--elevation-3)]"
        >
          <Dialog.Title className="font-heading font-medium text-base text-foreground">
            {state.status === "confirmed"
              ? t("connect.confirmed")
              : state.status === "expired"
                ? t("connect.expiredTitle")
                : state.status === "failed"
                  ? t("connect.failedTitle")
                  : t("connect.title", { platform })}
          </Dialog.Title>

          {(state.status === "idle" || state.status === "initiating") && (
            <p className="type-body flex items-center gap-2 text-muted-foreground">
              <Loader2 className="size-4 animate-spin" aria-hidden />
              {t("connect.initiating")}
            </p>
          )}

          {state.status === "awaiting" && state.artifact ? (
            <div className="flex flex-col gap-3" data-slot="connect-step">
              <PlatformStep artifact={state.artifact} meta={meta} />
              <p className="type-caption flex items-center gap-2 text-muted-foreground">
                <Loader2 className="size-3 animate-spin" aria-hidden />
                {t("connect.awaiting", { platform })}
              </p>
            </div>
          ) : null}

          {state.status === "confirmed" && (
            <p className="type-body flex items-center gap-2 text-foreground">
              <Check className="size-4 text-primary" aria-hidden />
              {t("connect.confirmedBody", { platform })}
            </p>
          )}

          {state.status === "expired" && (
            <p className="type-body text-muted-foreground">
              {t("connect.expiredBody")}
            </p>
          )}

          {state.status === "failed" && (
            <p className="type-body text-muted-foreground">
              {state.reason === "connector_unavailable"
                ? t("connect.unavailable", { platform })
                : t("connect.genericError")}
            </p>
          )}

          <div className="mt-2 flex justify-end gap-2">
            {state.status === "failed" && (
              <Button variant="default" size="sm" onClick={initiate}>
                {t("connect.failedRetry")}
              </Button>
            )}
            {state.status === "expired" && (
              <Button variant="default" size="sm" onClick={reissue}>
                {t("connect.reissue")}
              </Button>
            )}
            <Button
              variant={state.status === "confirmed" ? "default" : "outline"}
              size="sm"
              onClick={() => handleOpenChange(false)}
            >
              {state.status === "confirmed"
                ? t("connect.done")
                : t("connect.close")}
            </Button>
          </div>
        </Dialog.Popup>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
