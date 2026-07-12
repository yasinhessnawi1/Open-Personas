"use client";

import { useCallback, useState } from "react";
import { ConversationCalendar } from "./conversation-calendar";
import { ConversationFiles } from "./conversation-files";

type Panel = "files" | "calendar" | null;

/**
 * R9-024 — the chat header's Files + Calendar buttons, coordinated so only ONE of
 * their right-panel Sheets is ever open at a time (each button toggles its own; a
 * click on the other closes the first). Both are `page.tsx` (server component)
 * siblings, so the shared "which panel is open" state needs one client boundary —
 * this is it. Deliberately NOT the Spec-28 `FileRendererContext` (a different,
 * message-embedded-artifact concern; this coordinates only the two HEADER-triggered
 * browsers).
 */
export function ChatRightPanelGroup({
  personaId,
  conversationId,
  personaName,
}: {
  personaId: string;
  conversationId: string;
  personaName: string;
}) {
  const [openPanel, setOpenPanel] = useState<Panel>(null);

  const onFilesOpenChange = useCallback(
    (open: boolean) => setOpenPanel(open ? "files" : null),
    [],
  );
  const onCalendarOpenChange = useCallback(
    (open: boolean) => setOpenPanel(open ? "calendar" : null),
    [],
  );

  return (
    <>
      <ConversationFiles
        personaId={personaId}
        conversationId={conversationId}
        personaName={personaName}
        open={openPanel === "files"}
        onOpenChange={onFilesOpenChange}
      />
      <ConversationCalendar
        personaId={personaId}
        personaName={personaName}
        open={openPanel === "calendar"}
        onOpenChange={onCalendarOpenChange}
      />
    </>
  );
}
