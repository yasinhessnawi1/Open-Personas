/**
 * Spec 35 D-35-14 — command palette tests.
 *
 * Verifies: opens on the open-command-palette event; lists personas +
 * conversations; filters by query; navigates on select; the trigger shows the
 * (non-mac) Ctrl K label + dispatches the open event.
 */

import { fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  CommandPalette,
  CommandTrigger,
  OPEN_COMMAND_PALETTE_EVENT,
} from "./command-palette";
import { EMPTY_NAV_COUNTS, type SidebarData } from "./sidebar-data";

const push = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push }),
}));
const actions = vi.hoisted(() => ({
  startChat: vi.fn().mockResolvedValue(undefined),
  startVoice: vi.fn().mockResolvedValue(undefined),
}));
vi.mock("@/app/actions", () => actions);

const messages = {
  nav: {
    personas: "Personas",
    conversations: "Conversations",
    activity: "Activity",
    tasks: "Tasks",
    calls: "Calls",
    memory: "Memory",
    schedule: "Schedule",
    connectors: "Connected platforms",
    billing: "Billing",
    settings: "Settings",
    command: {
      open: "Search and commands",
      search: "Search",
      placeholder: "Search personas, conversations, or jump to…",
      empty: "No matches",
      groupRecent: "Recent",
      groupActions: "Actions",
      groupNavigate: "Go to",
      groupPersonas: "Personas",
      groupConversations: "Conversations",
      groupPersona: "Persona",
      newPersona: "New persona",
      newRoutine: "New routine",
      scopePlaceholder: "Do something with {name}…",
      clearScope: "Clear persona",
      actionChat: "Open chat with {name}",
      actionCall: "Call {name}",
      actionOpen: "Open {name}",
      actionEdit: "Edit {name}",
      actionFiles: "{name}'s files",
      footerNavigate: "navigate",
      footerOpen: "open",
      footerPersona: "persona actions",
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
  counts: EMPTY_NAV_COUNTS,
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

beforeEach(() => {
  window.localStorage.clear(); // recents persist per jsdom instance — isolate tests
  push.mockClear();
  actions.startChat.mockClear();
});

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
    expect(screen.getByText(/question/)).toBeTruthy();
    expect(screen.queryByText("Activity")).toBeNull();

    fireEvent.click(screen.getByRole("option", { name: /question/ }));
    expect(push).toHaveBeenCalledWith("/chat/conv_1");
  });

  it("drills into a persona (R11-B5): scope chip + real chat door", async () => {
    renderWith(<CommandPalette data={DATA} />);
    fireEvent(window, new Event(OPEN_COMMAND_PALETTE_EVENT));
    await screen.findByPlaceholderText(/search personas/i);

    // Enter the persona scope from the Personas group row (data-drill: the
    // accessible name concatenates avatar+label+sublabel without spaces).
    fireEvent.click(
      document.querySelector(
        '[data-drill="astrid_tenancy_law"]',
      ) as HTMLElement,
    );
    expect(
      screen.getByPlaceholderText("Do something with Astrid…"),
    ).toBeTruthy();

    // The scoped actions ride the REAL doors.
    fireEvent.click(
      screen.getByRole("option", { name: /Open chat with Astrid/ }),
    );
    expect(actions.startChat).toHaveBeenCalledWith("astrid_tenancy_law");
  });

  it("pops the scope with Backspace on an empty query", async () => {
    renderWith(<CommandPalette data={DATA} />);
    fireEvent(window, new Event(OPEN_COMMAND_PALETTE_EVENT));
    const input = await screen.findByPlaceholderText(/search personas/i);
    fireEvent.click(
      document.querySelector(
        '[data-drill="astrid_tenancy_law"]',
      ) as HTMLElement,
    );
    fireEvent.keyDown(screen.getByPlaceholderText(/do something/i), {
      key: "Backspace",
    });
    expect(input.getAttribute("placeholder")).toMatch(/search personas/i);
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

describe("billing entry point (R9-137)", () => {
  it("lists Billing and navigates to it", async () => {
    renderWith(<CommandPalette data={DATA} />);
    fireEvent(window, new Event(OPEN_COMMAND_PALETTE_EVENT));
    await screen.findByPlaceholderText(/search personas/i);

    fireEvent.click(screen.getByRole("option", { name: /Billing/ }));
    expect(push).toHaveBeenCalledWith("/settings/billing");
  });
});
