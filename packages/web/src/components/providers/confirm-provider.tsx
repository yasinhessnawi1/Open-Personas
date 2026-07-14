"use client";

/**
 * Spec 35 cluster M (D-35-12) — global confirmation system.
 *
 * `<ConfirmProvider>` mounts once in the app shell; `useConfirm()` returns an
 * async `confirm(options)` that resolves a promise off a single base-ui
 * `Dialog` (mirrors the [ui/sheet.tsx](../ui/sheet.tsx) base-ui wrapper). This
 * replaces every native `window.confirm()` (acceptance §4.5: zero native
 * dialogs) with the redesign's own token-styled modal — destructive flows pass
 * `tone: "danger"` to tint the confirm action.
 *
 * No new dependency: `@base-ui/react` already backs the sheet/dialog layer.
 */

import { Dialog } from "@base-ui/react/dialog";
import { useTranslations } from "next-intl";
import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useMemo,
  useState,
} from "react";
import { Button } from "@/components/ui/button";

/** The shape a caller passes to `confirm(...)`. */
export interface ConfirmOptions {
  /** The headline — what's about to happen. Required. */
  title: string;
  /** Optional supporting line (consequences, scope). */
  description?: string;
  /** Confirm-button label; defaults to the i18n "Confirm". */
  confirmLabel?: string;
  /** Cancel-button label; defaults to the i18n "Cancel". */
  cancelLabel?: string;
  /** "danger" tints the confirm action destructive (delete/duplicate flows). */
  tone?: "default" | "danger";
}

type ConfirmFn = (options: ConfirmOptions) => Promise<boolean>;

const ConfirmContext = createContext<ConfirmFn | null>(null);

/** Async confirmation; resolves `true` on confirm, `false` on cancel/dismiss. */
export function useConfirm(): ConfirmFn {
  const ctx = useContext(ConfirmContext);
  if (ctx === null) {
    throw new Error("useConfirm must be used within a <ConfirmProvider>");
  }
  return ctx;
}

interface Pending {
  options: ConfirmOptions;
  resolve: (ok: boolean) => void;
}

export function ConfirmProvider({ children }: { children: ReactNode }) {
  const t = useTranslations("confirm");
  const [pending, setPending] = useState<Pending | null>(null);

  const confirm = useCallback<ConfirmFn>(
    (options) =>
      new Promise<boolean>((resolve) => {
        setPending({ options, resolve });
      }),
    [],
  );

  // Settle exactly once: clear the pending entry and resolve its promise. A
  // second call (e.g. onOpenChange firing after the button click) sees a null
  // pending and is a no-op, so the promise never double-resolves.
  const settle = useCallback((ok: boolean) => {
    setPending((cur) => {
      cur?.resolve(ok);
      return null;
    });
  }, []);

  const value = useMemo(() => confirm, [confirm]);
  const options = pending?.options;
  const danger = options?.tone === "danger";

  return (
    <ConfirmContext.Provider value={value}>
      {children}
      <Dialog.Root
        open={pending !== null}
        onOpenChange={(open) => {
          // Backdrop click / Escape / programmatic close → treat as cancel.
          if (!open) settle(false);
        }}
      >
        <Dialog.Portal>
          <Dialog.Backdrop className="fixed inset-0 z-[80] bg-black/40 transition-opacity duration-[var(--motion-duration-fast)] data-ending-style:opacity-0 data-starting-style:opacity-0 supports-backdrop-filter:backdrop-blur-xs" />
          <Dialog.Popup
            data-slot="confirm-dialog"
            className="-translate-x-1/2 -translate-y-1/2 fixed top-1/2 left-1/2 z-[80] flex w-[min(28rem,calc(100vw-2rem))] flex-col gap-3 rounded-xl border bg-popover bg-clip-padding p-5 text-popover-foreground shadow-[var(--elevation-3)] transition duration-[var(--motion-duration-normal)] ease-[var(--motion-ease-emphasized)] data-ending-style:scale-95 data-ending-style:opacity-0 data-starting-style:scale-95 data-starting-style:opacity-0"
          >
            <Dialog.Title className="font-heading font-medium text-base text-foreground">
              {options?.title}
            </Dialog.Title>
            {options?.description ? (
              <Dialog.Description className="text-muted-foreground text-sm">
                {options.description}
              </Dialog.Description>
            ) : null}
            <div className="mt-2 flex justify-end gap-2">
              <Button variant="outline" onClick={() => settle(false)}>
                {options?.cancelLabel ?? t("cancel")}
              </Button>
              <Button
                variant={danger ? "destructive" : "default"}
                onClick={() => settle(true)}
                data-tone={danger ? "danger" : undefined}
              >
                {options?.confirmLabel ?? t("confirm")}
              </Button>
            </div>
          </Dialog.Popup>
        </Dialog.Portal>
      </Dialog.Root>
    </ConfirmContext.Provider>
  );
}
