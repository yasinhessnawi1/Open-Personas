"use client";

import { CalendarDays } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";
import { CalendarView } from "@/components/schedule/calendar-view";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import { personaIdentityStyle } from "@/lib/persona-identity";
import { cn } from "@/lib/utils";

/**
 * R9-024 — the chat header's Calendar button + right-panel instance: the persona's
 * schedule, reusing A8's `CalendarView` with the `personaId` filter (ONE component
 * serves `/schedule` and chat — no fork; view/reschedule/delete all work here the
 * same as on the full calendar page).
 *
 * R11-B3 rider — the kit's `schedule-persona.html` PANEL register: a narrow rail
 * (not the 60vw full calendar), the identity-dot + Fraunces "{name}'s schedule"
 * header with the mono THIS CHAT chip, and `variant="panel"` underneath (agenda
 * only, full-width New-routine locked to this persona, panel-voiced banner with
 * the pointer to the full calendar).
 *
 * Mirrors {@link import("./conversation-files").ConversationFiles}'s button + Sheet
 * shell (same icon-button treatment, same `side="right"` panel pattern) so the two
 * sit as visual siblings in the header. Controlled-open like its sibling — the parent
 * (`ChatRightPanelGroup`) coordinates the two so only one is open at a time.
 */
export function ConversationCalendar({
  personaId,
  personaName,
  personaAvatarUrl,
  open: openProp,
  onOpenChange,
}: {
  personaId: string;
  personaName: string;
  personaAvatarUrl?: string | null;
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
          className="w-full gap-0 overflow-y-auto p-0 sm:w-[420px] sm:max-w-md"
          data-slot="conversation-calendar-panel"
        >
          {/* kit header: identity dot · Fraunces "{name}'s schedule" · THIS CHAT chip */}
          <header
            className="flex items-center gap-2.5 border-b border-border px-4 py-3"
            style={personaIdentityStyle({ id: personaId })}
          >
            <span
              aria-hidden="true"
              className="size-2.5 shrink-0 rounded-[3px]"
              style={{
                background: "var(--v-id)",
                boxShadow:
                  "0 0 0 3px color-mix(in oklch, var(--v-id) 18%, transparent)",
              }}
            />
            <SheetTitle className="min-w-0 flex-1 truncate font-heading text-base font-semibold tracking-tight">
              {t("title", { name: personaName })}
            </SheetTitle>
            <span className="type-caption mr-6 shrink-0 text-muted-foreground">
              {t("thisChat")}
            </span>
          </header>
          <div className="min-h-0 flex-1 px-4 py-3">
            <CalendarView
              personaId={personaId}
              variant="panel"
              panelPersona={{
                id: personaId,
                name: personaName,
                avatar_url: personaAvatarUrl,
              }}
            />
          </div>
        </SheetContent>
      </Sheet>
    </>
  );
}
