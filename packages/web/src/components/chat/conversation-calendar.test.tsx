/**
 * R9-024 — <ConversationCalendar> (the chat header's Calendar button + right panel).
 *
 * Verifies: the header button renders + opens the panel; the underlying
 * `<CalendarView>` receives the `personaId` filter (mocked — CalendarView's own
 * behaviour is covered by calendar-view.test.tsx); the panel is controllable
 * (open/onOpenChange) the same way <ConversationFiles> is, for the
 * one-panel-at-a-time coordination in <ChatRightPanelGroup>.
 */

import { fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it, vi } from "vitest";
import { ConversationCalendar } from "./conversation-calendar";

const h = vi.hoisted(() => ({ onOpenChange: vi.fn() }));

vi.mock("@/components/schedule/calendar-view", () => ({
  CalendarView: ({ personaId }: { personaId?: string }) => (
    <div data-testid="calendar-view-stub">{personaId}</div>
  ),
}));

const messages = {
  chat: {
    calendar: {
      button: "Calendar",
      title: "{name}'s schedule",
    },
  },
};

function renderCalendar(props: Partial<{ open: boolean }> = {}) {
  return render(
    <NextIntlClientProvider locale="en" messages={messages}>
      <ConversationCalendar
        personaId="mara"
        personaName="Mara"
        onOpenChange={h.onOpenChange}
        {...props}
      />
    </NextIntlClientProvider>,
  );
}

describe("ConversationCalendar", () => {
  it("renders the header button, closed by default (uncontrolled)", () => {
    render(
      <NextIntlClientProvider locale="en" messages={messages}>
        <ConversationCalendar personaId="mara" personaName="Mara" />
      </NextIntlClientProvider>,
    );
    expect(
      screen.getByRole("button", { name: "Calendar" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("Mara's schedule")).not.toBeInTheDocument();
  });

  it("clicking the button opens the panel with the persona-scoped CalendarView", () => {
    renderCalendar({ open: false });
    fireEvent.click(screen.getByRole("button", { name: "Calendar" }));
    expect(h.onOpenChange).toHaveBeenCalledWith(true);
  });

  it("open=true renders the panel title + threads personaId into CalendarView", () => {
    renderCalendar({ open: true });
    expect(screen.getByText("Mara's schedule")).toBeInTheDocument();
    expect(screen.getByTestId("calendar-view-stub").textContent).toBe("mara");
  });
});
