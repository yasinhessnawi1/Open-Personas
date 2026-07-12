/**
 * R9-024 — <ChatRightPanelGroup>: the Files + Calendar header buttons share ONE
 * "which panel is open" state, so opening either one closes the other (never
 * both Sheets open at once).
 */

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ChatRightPanelGroup } from "./chat-right-panel-group";

vi.mock("./conversation-files", () => ({
  ConversationFiles: ({
    open,
    onOpenChange,
  }: {
    open: boolean;
    onOpenChange: (open: boolean) => void;
  }) => (
    <>
      <button type="button" onClick={() => onOpenChange(true)}>
        Files
      </button>
      {open ? <div data-testid="files-panel">files open</div> : null}
    </>
  ),
}));

vi.mock("./conversation-calendar", () => ({
  ConversationCalendar: ({
    open,
    onOpenChange,
  }: {
    open: boolean;
    onOpenChange: (open: boolean) => void;
  }) => (
    <>
      <button type="button" onClick={() => onOpenChange(true)}>
        Calendar
      </button>
      {open ? <div data-testid="calendar-panel">calendar open</div> : null}
    </>
  ),
}));

function renderGroup() {
  return render(
    <ChatRightPanelGroup
      personaId="mara"
      conversationId="conv_1"
      personaName="Mara"
    />,
  );
}

describe("ChatRightPanelGroup — one right panel at a time (R9-024)", () => {
  it("neither panel is open initially", () => {
    renderGroup();
    expect(screen.queryByTestId("files-panel")).toBeNull();
    expect(screen.queryByTestId("calendar-panel")).toBeNull();
  });

  it("opening Files opens only Files", () => {
    renderGroup();
    fireEvent.click(screen.getByText("Files"));
    expect(screen.getByTestId("files-panel")).toBeInTheDocument();
    expect(screen.queryByTestId("calendar-panel")).toBeNull();
  });

  it("opening Calendar after Files closes Files (only one panel at a time)", () => {
    renderGroup();
    fireEvent.click(screen.getByText("Files"));
    expect(screen.getByTestId("files-panel")).toBeInTheDocument();

    fireEvent.click(screen.getByText("Calendar"));
    expect(screen.getByTestId("calendar-panel")).toBeInTheDocument();
    expect(screen.queryByTestId("files-panel")).toBeNull();
  });
});
