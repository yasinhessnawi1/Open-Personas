"use client";

import { CalendarDays } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";
import { CalendarView } from "@/components/schedule/calendar-view";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import { cn } from "@/lib/utils";

/**
 * R9-024 — the chat header's Calendar button + right-panel instance: the persona's
 * schedule, reusing A8's `CalendarView` with the `personaId` filter (ONE component
 * serves `/schedule` and chat — no fork; view/reschedule/delete all work here the
 * same as on the full calendar page).
 *
 * Mirrors {@link import("./conversation-files").ConversationFiles}'s button + Sheet
 * shell (same icon-button treatment, same `side="right"` panel pattern) so the two
 * sit as visual siblings in the header. Controlled-open like its sibling — the parent
 * (`ChatRightPanelGroup`) coordinates the two so only one is open at a time.
 */
export function ConversationCalendar({
  personaId,
  personaName,
  open: openProp,
  onOpenChange,
}: {
  personaId: string;
  personaName: string;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
}) {
  const t = useTranslations("chat.calendar");
  const [openState, setOpenState] = useState(false);
  const open = openProp ?? openState;
  const setOpen = onOpenChange ?? setOpenState;

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        aria-label={t("button")}
        title={t("button")}
        className={cn(
          "grid size-9 shrink-0 place-items-center rounded-md border border-border text-muted-foreground",
          "hover:bg-muted hover:text-foreground",
          "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring",
        )}
        data-slot="conversation-calendar-button"
      >
        <CalendarDays className="size-4" aria-hidden />
      </button>

      <Sheet open={open} onOpenChange={setOpen}>
        <SheetContent
          side="right"
          showCloseButton
          className="w-full gap-0 overflow-y-auto p-0 sm:w-[60vw] sm:!max-w-5xl"
          data-slot="conversation-calendar-panel"
        >
          <header className="flex items-center gap-2 border-b border-border px-4 py-3">
            <CalendarDays
              className="size-4 shrink-0 text-muted-foreground"
              aria-hidden
            />
            <SheetTitle className="type-ui min-w-0 flex-1 truncate">
              {t("title", { name: personaName })}
            </SheetTitle>
          </header>
          <div className="min-h-0 flex-1 px-4 py-3">
            <CalendarView personaId={personaId} />
          </div>
        </SheetContent>
      </Sheet>
    </>
  );
}
