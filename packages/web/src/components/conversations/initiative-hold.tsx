"use client";

import { Dialog } from "@base-ui/react/dialog";
import { MessageCircle, MessageCircleOff } from "lucide-react";
import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";
import { useNotify } from "@/components/providers/notification-provider";
import { Button } from "@/components/ui/button";
import { DropdownMenuItem } from "@/components/ui/dropdown-menu";
import { ApiError, unwrap } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import { useApi } from "@/lib/api/use-api";

/**
 * D-OWNER-11-18 (16), D-ORCH-11-18-u: the per-conversation "fine to bring
 * this up" control.
 *
 * A conversation in which the user shared something sensitive is kept out of
 * what the persona starts on its own. The row menu reads the control when it
 * opens (so the list read stays as it is, and the row itself shows nothing)
 * and offers a release for a held conversation or the undo for a released
 * one. A conversation nothing holds shows no item. The release itself waits
 * for {@link ReleaseConfirmDialog}; the undo goes out at once.
 */

export type InitiativeHold = components["schemas"]["InitiativeHoldResponse"];

export const INITIATIVE_HOLD_PATH =
  "/v1/conversations/{conversation_id}/initiative-hold";

/** A release awaiting the confirm: what the dialog names and sends back. */
export interface PendingRelease {
  conversationId: string;
  personaName: string;
  version: number;
}

export function InitiativeHoldMenuItem({
  conversationId,
  personaName,
  onRelease,
  onHold,
}: {
  conversationId: string;
  personaName: string;
  onRelease: (pending: PendingRelease) => void;
  onHold: (conversationId: string, personaName: string) => void;
}) {
  const t = useTranslations("conversations");
  const api = useApi();
  const [hold, setHold] = useState<InitiativeHold | null>(null);

  useEffect(() => {
    let live = true;
    (async () => {
      try {
        const result = await api.GET(INITIATIVE_HOLD_PATH, {
          params: { path: { conversation_id: conversationId } },
        });
        const read = await unwrap(result);
        if (live) setHold(read);
      } catch {
        // A read that fails offers nothing: the item is optional, and the
        // conversation simply stays as it is.
        if (live) setHold(null);
      }
    })();
    return () => {
      live = false;
    };
  }, [api, conversationId]);

  if (hold === null || hold.state === "not_held") return null;
  if (hold.state === "held") {
    const version = hold.version;
    return (
      <DropdownMenuItem
        onClick={() => onRelease({ conversationId, personaName, version })}
        data-slot="initiative-release-item"
      >
        <MessageCircle className="mr-2 size-4" />
        {t("releaseItem", { name: personaName })}
      </DropdownMenuItem>
    );
  }
  return (
    <DropdownMenuItem
      onClick={() => onHold(conversationId, personaName)}
      data-slot="initiative-hold-item"
    >
      <MessageCircleOff className="mr-2 size-4" />
      {t("holdItem", { name: personaName })}
    </DropdownMenuItem>
  );
}

/**
 * The release and the undo, as the owner. A release carries the version the
 * menu read; a 409 means something new was shared there since, so it stays
 * held and the user is told to look again (never retried silently).
 */
export function useInitiativeHoldActions() {
  const t = useTranslations("conversations");
  const api = useApi();
  const { notify } = useNotify();
  const [pending, setPending] = useState<PendingRelease | null>(null);

  async function send(
    conversationId: string,
    body: { released: boolean; version?: number },
  ) {
    const result = await api.PUT(INITIATIVE_HOLD_PATH, {
      params: { path: { conversation_id: conversationId } },
      body,
    });
    return unwrap(result);
  }

  function failed(e: unknown) {
    if (e instanceof ApiError && e.status === 409) {
      notify({ level: "warning", title: t("holdChanged") });
    } else {
      notify({ level: "error", title: t("holdFailed") });
    }
  }

  async function confirmRelease() {
    if (!pending) return;
    const { conversationId, personaName, version } = pending;
    setPending(null);
    try {
      await send(conversationId, { released: true, version });
      notify({ level: "success", title: t("released", { name: personaName }) });
    } catch (e) {
      failed(e);
    }
  }

  async function holdAgain(conversationId: string, personaName: string) {
    try {
      await send(conversationId, { released: false });
      notify({ level: "info", title: t("heldAgain", { name: personaName }) });
    } catch (e) {
      failed(e);
    }
  }

  return { pending, setPending, confirmRelease, holdAgain };
}

export function ReleaseConfirmDialog({
  pending,
  onCancel,
  onConfirm,
}: {
  pending: PendingRelease | null;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const t = useTranslations("conversations");
  const tc = useTranslations("confirm");
  const name = pending?.personaName ?? "";
  return (
    <Dialog.Root
      open={pending !== null}
      onOpenChange={(open) => {
        if (!open) onCancel();
      }}
    >
      <Dialog.Portal>
        <Dialog.Backdrop className="fixed inset-0 z-[80] bg-black/40 transition-opacity duration-[var(--motion-duration-fast)] data-ending-style:opacity-0 data-starting-style:opacity-0 supports-backdrop-filter:backdrop-blur-xs" />
        <Dialog.Popup
          data-slot="release-conversation-dialog"
          className="-translate-x-1/2 -translate-y-1/2 fixed top-1/2 left-1/2 z-[80] flex w-[min(28rem,calc(100vw-2rem))] flex-col gap-3 rounded-xl border bg-popover bg-clip-padding p-5 text-popover-foreground shadow-[var(--elevation-3)]"
        >
          <Dialog.Title className="font-heading font-medium text-base text-foreground">
            {t("releaseTitle", { name })}
          </Dialog.Title>
          <Dialog.Description className="text-muted-foreground text-sm">
            {t("releaseBody", { name })}
          </Dialog.Description>
          <div className="mt-2 flex justify-end gap-2">
            <Button variant="outline" onClick={onCancel}>
              {tc("cancel")}
            </Button>
            <Button onClick={onConfirm}>{t("releaseConfirm")}</Button>
          </div>
        </Dialog.Popup>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
