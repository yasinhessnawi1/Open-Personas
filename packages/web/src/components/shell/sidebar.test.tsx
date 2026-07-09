/**
 * Structural tests for the desktop app-sidebar layout contract.
 *
 * These lock in the bottom-pinned-footer fix: the sidebar is a viewport-height
 * flex column, the MESSAGES region is the only internal scroll area (so a long
 * conversation list scrolls WITHIN the sidebar), and the Settings entry sits in
 * a non-shrinking footer that stays visible no matter how many conversations
 * exist — in both expanded and collapsed (icon-rail) modes.
 */
import { render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it, vi } from "vitest";
import { NotificationProvider } from "@/components/providers/notification-provider";
import { Sidebar } from "./sidebar";
import {
  EMPTY_NAV_COUNTS,
  type SidebarConversation,
  type SidebarData,
} from "./sidebar-data";

vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ getToken: async () => null }),
  useUser: () => ({ user: null }),
  useClerk: () => ({ signOut: vi.fn(), openUserProfile: vi.fn() }),
}));

vi.mock("next/navigation", () => ({
  usePathname: () => "/",
}));

const messages = {
  nav: {
    primary: "Primary",
    home: "Home",
    personas: "Personas",
    conversations: "Conversations",
    calls: "Calls",
    activity: "Activity",
    memory: "Memory",
    schedule: "Schedule",
    newPersona: "New persona",
    settings: "Settings",
    sidebar: {
      collapse: "Collapse",
      expand: "Expand",
      resize: "Resize sidebar",
      personas: "Personas",
      messages: "Messages",
      messagesEmpty: "No conversations yet",
      allChats: "{count, plural, =0 {All chats} other {All chats (#)}}",
      untitled: "Untitled conversation",
      unknownPersona: "Unknown persona",
    },
    command: { open: "Search and commands", search: "Search" },
    account: { menu: "Account", plan: "Account" },
  },
  theme: { light: "Light", dark: "Dark", system: "System" },
  notifications: {
    open: "Notifications",
    title: "Notifications",
    empty: "Nothing yet.",
    clear: "Clear all",
    unreadLabel: "{count} unread",
  },
};

/** A long conversation list — the case that used to push Settings off-screen. */
const manyConversations: SidebarConversation[] = Array.from(
  { length: 50 },
  (_, i) => ({
    id: `c${i}`,
    title: `Conversation ${i}`,
    updated_at: "2026-06-10T00:00:00Z",
    persona: null,
  }),
);

const data: SidebarData = {
  personas: [],
  conversations: manyConversations,
  calls: [],
  counts: EMPTY_NAV_COUNTS,
  ownerName: null,
  memoryAvailable: false,
};

function wrap(ui: React.ReactNode) {
  return render(
    <NextIntlClientProvider locale="en" messages={messages}>
      <NotificationProvider>{ui}</NotificationProvider>
    </NextIntlClientProvider>,
  );
}

describe("Sidebar layout contract", () => {
  it("caps the column to the viewport height so it never grows past the screen", () => {
    const { container } = wrap(<Sidebar data={data} />);
    const aside = container.querySelector<HTMLElement>(
      '[data-slot="app-shell-sidebar"]',
    );
    expect(aside).not.toBeNull();
    // A fixed full-viewport-height, sticky flex column.
    expect(aside?.className).toContain("md:h-svh");
    expect(aside?.className).toContain("md:sticky");
    expect(aside?.className).toContain("md:flex-col");
  });

  it("makes MESSAGES the only internal scroll region (min-h-0 + flex-1)", () => {
    const { container } = wrap(<Sidebar data={data} />);
    // The MESSAGES section is the grow region; its ScrollArea scrolls within.
    const scrollArea = container.querySelector<HTMLElement>(
      '[data-slot="scroll-area"]',
    );
    expect(scrollArea).not.toBeNull();
    expect(scrollArea?.className).toContain("min-h-0");
    expect(scrollArea?.className).toContain("flex-1");
  });

  it("does NOT render a Calls preview list even when calls exist (R4 T2)", () => {
    const withCalls: SidebarData = {
      ...data,
      calls: [
        {
          callId: "call1",
          conversationId: "conv1",
          startedAt: "2026-06-10T00:00:00Z",
          durationS: 90,
          persona: null,
        },
      ],
    };
    const { container } = wrap(<Sidebar data={withCalls} />);
    expect(
      container.querySelector('[data-slot="sidebar-calls-list"]'),
    ).toBeNull();
  });

  it("shows honest count badges on nav rows and hides zero counts (R9-010)", () => {
    const withCounts: SidebarData = {
      ...data,
      counts: {
        personas: 12,
        conversations: 34,
        calls: 5,
        memoryNodes: 0,
        activeTasks: 2,
        schedules: 3,
      },
      memoryAvailable: true,
    };
    wrap(<Sidebar data={withCounts} />);
    const nav = screen.getByRole("navigation", { name: "Primary" });
    const rowText = (label: string) =>
      Array.from(nav.querySelectorAll("a")).find((a) =>
        a.textContent?.includes(label),
      )?.textContent;
    // Badges come from the honest owner totals (not the truncated previews:
    // data.personas is empty here, yet the badge reads 12).
    expect(rowText("Personas")).toContain("12");
    expect(rowText("Calls")).toContain("5");
    expect(rowText("Activity")).toContain("2");
    expect(rowText("Schedule")).toContain("3");
    // Zero-hidden: the Memory row renders, its zero count does not.
    expect(rowText("Memory")).toBe("Memory");
  });

  it("folds Conversations into MESSAGES: no nav row, an All chats (N) link (R9-009)", () => {
    const withCounts: SidebarData = {
      ...data,
      counts: { ...data.counts, conversations: 34 },
    };
    wrap(<Sidebar data={withCounts} />);
    // The primary nav no longer carries a /conversations row…
    const nav = screen.getByRole("navigation", { name: "Primary" });
    expect(
      Array.from(nav.querySelectorAll("a")).some(
        (a) => a.getAttribute("href") === "/conversations",
      ),
    ).toBe(false);
    // …the MESSAGES section header carries the affordance instead, with the
    // same honest total the removed row's badge showed.
    const allChats = screen.getByRole("link", { name: "All chats (34)" });
    expect(allChats.getAttribute("href")).toBe("/conversations");
  });

  it("keeps /conversations reachable in the collapsed icon rail (R9-009)", () => {
    localStorage.setItem("persona:sidebar-collapsed", "true");
    try {
      wrap(<Sidebar data={data} />);
      // Zero conversations → the plural label collapses to "All chats".
      const allChats = screen.getByRole("link", { name: "All chats" });
      expect(allChats.getAttribute("href")).toBe("/conversations");
    } finally {
      localStorage.removeItem("persona:sidebar-collapsed");
    }
  });

  it("pins the account footer (non-shrinking) so it stays present with a long list", () => {
    // Spec 35 D-35-16: the footer is now the custom account menu, not a bare
    // Settings link (settings moved inside the account menu).
    wrap(<Sidebar data={data} />);
    const account = screen.getByRole("button", { name: "Account" });
    // The footer wrapper is non-shrinking and bottom-pinned.
    const footer = account.closest<HTMLElement>(".shrink-0");
    expect(footer).not.toBeNull();
    expect(footer?.className).toContain("mt-auto");
  });
});
