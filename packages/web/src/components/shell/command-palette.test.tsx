/**
 * Spec 35 D-35-14 — command palette tests.
 *
 * Verifies: opens on the open-command-palette event; lists personas +
 * conversations; filters by query; navigates on select; the trigger shows the
 * (non-mac) Ctrl K label + dispatches the open event.
 */

import { fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it, vi } from "vitest";
import {
  CommandPalette,
  CommandTrigger,
  OPEN_COMMAND_PALETTE_EVENT,
} from "./command-palette";
import type { SidebarData } from "./sidebar-data";

const push = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push }),
}));

const messages = {
  nav: {
    home: "Home",
    personas: "Personas",
    conversations: "Conversations",
    command: {
      open: "Search and commands",
      search: "Search",
      placeholder: "Search personas, conversations, or jump to…",
      empty: "No matches",
      groupActions: "Actions",
      groupNavigate: "Go to",
      groupPersonas: "Personas",
      groupConversations: "Conversations",
      newPersona: "New persona",
      hint: "to open",
    },
  },
};

const ASTRID = {
  id: "astrid_tenancy_law",
  name: "Astrid",
  role: "Tenancy law assistant",
  created_at: "2026-01-01T00:00:00Z",
} as const;

const DATA: SidebarData = {
  personas: [ASTRID],
  conversations: [
    {
      id: "conv_1",
      title: "Lease question",
      updated_at: "2026-06-01T00:00:00Z",
      persona: ASTRID,
    },
  ],
  calls: [],
  ownerName: null,
  memoryAvailable: false,
};

function renderWith(node: React.ReactNode) {
  return render(
    <NextIntlClientProvider locale="en" messages={messages}>
      {node}
    </NextIntlClientProvider>,
  );
}

describe("CommandPalette", () => {
  it("opens on the event and lists personas + conversations", async () => {
    renderWith(<CommandPalette data={DATA} />);
    expect(screen.queryByPlaceholderText(/search personas/i)).toBeNull();

    fireEvent(window, new Event(OPEN_COMMAND_PALETTE_EVENT));

    const input = await screen.findByPlaceholderText(/search personas/i);
    expect(input).toBeTruthy();
    expect(screen.getAllByText("Astrid").length).toBeGreaterThan(0);
    expect(screen.getByText("Lease question")).toBeTruthy();
  });

  it("filters by query and navigates on select", async () => {
    renderWith(<CommandPalette data={DATA} />);
    fireEvent(window, new Event(OPEN_COMMAND_PALETTE_EVENT));
    const input = await screen.findByPlaceholderText(/search personas/i);

    fireEvent.change(input, { target: { value: "lease" } });
    expect(screen.getByText("Lease question")).toBeTruthy();
    expect(screen.queryByText("Home")).toBeNull();

    fireEvent.click(screen.getByText("Lease question"));
    expect(push).toHaveBeenCalledWith("/chat/conv_1");
  });
});

describe("CommandTrigger", () => {
  it("renders the non-mac Ctrl K label and dispatches the open event on click", async () => {
    const onOpen = vi.fn();
    window.addEventListener(OPEN_COMMAND_PALETTE_EVENT, onOpen);
    renderWith(<CommandTrigger />);

    expect(await screen.findByText("Ctrl K")).toBeTruthy();
    fireEvent.click(screen.getByRole("button"));
    expect(onOpen).toHaveBeenCalled();
    window.removeEventListener(OPEN_COMMAND_PALETTE_EVENT, onOpen);
  });
});
