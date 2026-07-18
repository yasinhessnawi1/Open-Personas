"use client";

import { useTranslations } from "next-intl";
import { type KeyboardEvent, type MouseEvent, useState } from "react";
import { EpisodicManagerModal } from "@/components/memory/episodic-manager-modal";
import type { AvatarPersona } from "@/components/persona/persona-avatar";

/**
 * Spec K11 (T5, D-K11-7c) — the chat header's `remembers N` becomes a button
 * opening the persona-scoped episodic manager. It renders inside the header's
 * persona `<Link>` (Spec 35's `openPersona` click target), so a real `<button>`
 * can't nest there (invalid content model, and its click would also trigger
 * the Link's navigation) — a `role="button"` span drives
 * `EpisodicManagerModal` via its controlled `open`/`onOpenChange` pair
 * instead of relying on `Dialog.Trigger`'s built-in activation: Base UI's
 * `useButton()` defaults `nativeButton` to `true`, so its Enter/Space→click
 * polyfill never fires for a non-native-button host, leaving the span
 * mouse-only. Click AND keydown (Enter/Space) explicitly open the modal,
 * `stopPropagation` keeps the click isolated from the ancestor Link (mirrors
 * `chat/file-card.tsx`'s nested-interactive pattern).
 */
export function ChatRemembersButton({
  persona,
  count,
}: {
  persona: AvatarPersona;
  count: number;
}) {
  const tc = useTranslations("chat");
  const [open, setOpen] = useState(false);

  const openModal = (e: MouseEvent | KeyboardEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setOpen(true);
  };

  return (
    <EpisodicManagerModal
      personas={[persona]}
      open={open}
      onOpenChange={setOpen}
      trigger={
        // biome-ignore lint/a11y/useSemanticElements: nests inside the header's persona <Link>; a <button> is invalid there and would also trigger navigation. role/tabIndex/keydown drive EpisodicManagerModal's controlled open state explicitly (Dialog.Trigger's Enter/Space polyfill assumes a native <button> host).
        <span
          role="button"
          tabIndex={0}
          className="cursor-pointer underline-offset-2 hover:underline"
          style={{ color: "var(--store-self-facts)" }}
          onClick={openModal}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") {
              if (e.key === " ") e.preventDefault();
              openModal(e);
            }
          }}
        >
          {tc("remembers", { count })}
        </span>
      }
    />
  );
}
