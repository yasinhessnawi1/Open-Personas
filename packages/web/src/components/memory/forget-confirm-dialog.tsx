"use client";

import { Dialog } from "@base-ui/react/dialog";
import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import type { ForgetCandidate } from "@/lib/api";

/**
 * Spec K11 (T5, D-K11-1 / D-K11-4) — the cross-layer forget confirm. A
 * concept-node delete previews the episodic evidence it would also erase
 * (`forget-preview`); when candidates come back, THIS dialog is the confirm —
 * persona-labelled, each deselectable — before anything is committed
 * (`forget`). Every candidate starts selected (= will be forgotten); a
 * deselect keeps that one piece of evidence alive. No candidates → the caller
 * falls back to the plain `DELETE /nodes/{id}` (D-K11-4), never reaching this
 * dialog at all.
 */
export function ForgetConfirmDialog({
  open,
  candidates,
  busy,
  onCancel,
  onConfirm,
}: {
  open: boolean;
  candidates: readonly ForgetCandidate[];
  busy: boolean;
  onCancel: () => void;
  onConfirm: (kept: readonly ForgetCandidate[]) => void;
}) {
  const t = useTranslations("memory");
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());

  // Reset to "all selected" (everything shown is forgotten by default; D-K11-1)
  // whenever a fresh candidate set opens.
  useEffect(() => {
    if (open) setSelected(new Set(candidates.map((c) => c.chunk_id)));
  }, [open, candidates]);

  const toggle = (chunkId: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(chunkId)) next.delete(chunkId);
      else next.add(chunkId);
      return next;
    });
  };

  return (
    <Dialog.Root
      open={open}
      onOpenChange={(next) => {
        if (!next) onCancel();
      }}
    >
      <Dialog.Portal>
        <Dialog.Backdrop className="fixed inset-0 z-[70] bg-black/40 transition-opacity duration-[var(--motion-duration-fast)] data-ending-style:opacity-0 data-starting-style:opacity-0 supports-backdrop-filter:backdrop-blur-xs" />
        <Dialog.Popup
          data-slot="forget-confirm-dialog"
          className="-translate-x-1/2 -translate-y-1/2 fixed top-1/2 left-1/2 z-[70] flex max-h-[75vh] w-[min(30rem,calc(100vw-2rem))] flex-col gap-3 rounded-xl border bg-popover bg-clip-padding p-5 text-popover-foreground shadow-[var(--elevation-3)]"
        >
          <Dialog.Title className="font-heading font-medium text-base text-foreground">
            {t("forgetDialogTitle", { count: candidates.length })}
          </Dialog.Title>
          <Dialog.Description className="text-muted-foreground text-sm">
            {t("forgetDialogBody")}
          </Dialog.Description>

          <ul className="flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto">
            {candidates.map((c) => (
              <li key={c.chunk_id}>
                <label className="flex items-start gap-2.5 rounded-lg border border-border bg-background p-2.5 text-sm">
                  <input
                    type="checkbox"
                    className="mt-0.5"
                    checked={selected.has(c.chunk_id)}
                    onChange={() => toggle(c.chunk_id)}
                    disabled={busy}
                  />
                  <span className="min-w-0 flex-1">
                    <span className="block font-medium">
                      {c.persona_name ?? c.persona_id}
                    </span>
                    <span className="mt-0.5 block line-clamp-2 text-muted-foreground">
                      {c.text}
                    </span>
                  </span>
                </label>
              </li>
            ))}
          </ul>

          <div className="mt-2 flex justify-end gap-2">
            <Button variant="outline" onClick={onCancel} disabled={busy}>
              {t("forgetDialogCancel")}
            </Button>
            <Button
              variant="destructive"
              disabled={busy}
              onClick={() =>
                onConfirm(candidates.filter((c) => selected.has(c.chunk_id)))
              }
            >
              {t("forgetDialogConfirm")}
            </Button>
          </div>
        </Dialog.Popup>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
