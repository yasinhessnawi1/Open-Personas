"use client";

import { Dialog } from "@base-ui/react/dialog";
import { X } from "lucide-react";
import { useTranslations } from "next-intl";
import { type ReactElement, useState } from "react";
import type { AvatarPersona } from "@/components/persona/persona-avatar";
import { EpisodicGraph } from "./episodic-graph";

/**
 * Spec K11 (T5, D-K11-7) — the episodic-manager modal: THE one reusable mount
 * for `<EpisodicGraph>` off the two non-`/memory` entry points (the persona
 * glance's Conversations row, the chat header's `remembers N`). `/memory`'s
 * own `[ Concept | Episodic ]` toggle (T4, `memory-view.tsx`) mounts
 * `<EpisodicGraph>` inline instead — this modal is purely for surfaces that
 * aren't already on the Memory page.
 *
 * Uncontrolled by default (mirrors `<PersonaMemoriesModal>`'s `trigger` prop);
 * `open`/`onOpenChange` let a caller drive it from a control that can't itself
 * be the `Dialog.Trigger` (K11-T5's chat-header button, nested inside a
 * `<Link>`, closes over its own stopPropagation instead).
 */
export function EpisodicManagerModal({
  personas,
  trigger,
  open: controlledOpen,
  onOpenChange,
}: {
  personas: readonly AvatarPersona[];
  trigger?: ReactElement;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
}) {
  const t = useTranslations("memory");
  const [uncontrolledOpen, setUncontrolledOpen] = useState(false);
  const open = controlledOpen ?? uncontrolledOpen;
  const setOpen = onOpenChange ?? setUncontrolledOpen;

  return (
    <Dialog.Root open={open} onOpenChange={setOpen}>
      {trigger ? <Dialog.Trigger render={trigger} /> : null}
      <Dialog.Portal>
        <Dialog.Backdrop className="fixed inset-0 z-50 bg-black/30 transition-opacity duration-[var(--motion-duration-fast)] data-ending-style:opacity-0 data-starting-style:opacity-0 supports-backdrop-filter:backdrop-blur-xs" />
        <Dialog.Popup className="-translate-x-1/2 -translate-y-1/2 fixed top-1/2 left-1/2 z-50 flex h-[85vh] w-[min(64rem,calc(100vw-2rem))] flex-col overflow-hidden rounded-2xl border border-border bg-card p-4 shadow-xl outline-none">
          <div className="flex items-center justify-between pb-3">
            <Dialog.Title className="font-heading text-lg font-semibold">
              {t("episodicManagerTitle")}
            </Dialog.Title>
            <Dialog.Close
              render={
                <button
                  type="button"
                  aria-label={t("close")}
                  className="grid size-8 place-items-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground"
                >
                  <X className="size-4" />
                </button>
              }
            />
          </div>
          <div className="flex min-h-0 flex-1 flex-col">
            <EpisodicGraph personas={personas} />
          </div>
        </Dialog.Popup>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
