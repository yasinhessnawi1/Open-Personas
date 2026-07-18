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
 *
 * R9-054 — the span is rendered as a plain sibling, NOT passed as
 * `EpisodicManagerModal`'s `trigger` prop. Since this component already fully
 * self-drives the modal via the controlled `open`/`onOpenChange` pair (and
 * handles its own Enter/Space activation above), feeding the span into
 * `Dialog.Trigger` was pure redundancy — and `Dialog.Trigger` defaults
 * `nativeButton` to `true`, so rendering a non-`<button>` host through it
 * emitted a Base UI console warning on every ChatPage mount. Dropping the
 * `trigger` prop removes the unused `Dialog.Trigger` (and the warning)
 * without touching `EpisodicManagerModal`'s default for its other callers.
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
    <>
      {/* biome-ignore lint/a11y/useSemanticElements: nests inside the header's persona <Link>; a <button> is invalid there and would also trigger navigation. role/tabIndex/keydown drive EpisodicManagerModal's controlled open state explicitly. */}
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
      <EpisodicManagerModal
        personas={[persona]}
        open={open}
        onOpenChange={setOpen}
      />
    </>
  );
}
