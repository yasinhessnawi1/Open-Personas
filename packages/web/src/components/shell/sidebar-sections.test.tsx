/**
 * Structural tests for the app-sidebar section bodies.
 *
 * Asserts the chat-app MESSAGES contract (title = persona name, brief =
 * conversation title, untitled/unknown fallbacks, active row, empty state,
 * collapsed-mode label suppression) and the PERSONAS rail links.
 */
import { render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it, vi } from "vitest";
import type {
  SidebarCall,
  SidebarConversation,
  SidebarPersona,
} from "./sidebar-data";
import {
  AllChatsLink,
  CallsList,
  MessagesList,
  PersonasRail,
} from "./sidebar-sections";

vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ getToken: async () => null }),
}));

let pathname = "/";
vi.mock("next/navigation", () => ({
  usePathname: () => pathname,
}));

const messages = {
  nav: {
    sidebar: {
      messagesEmpty: "No conversations yet",
      allChats: "{count, plural, =0 {All chats} other {All chats (#)}}",
      untitled: "Untitled conversation",
      unknownPersona: "Unknown persona",
      callsEmpty: "No calls yet",
      callOngoing: "Call",
    },
  },
};

const astrid: SidebarPersona = {
  id: "astrid",
  name: "Astrid",
  role: "Tenancy",
  created_at: "2026-01-01",
  avatar_url: null,
};

function wrap(ui: React.ReactNode) {
  return render(
    <NextIntlClientProvider locale="en" messages={messages}>
      {ui}
    </NextIntlClientProvider>,
  );
}

describe("AllChatsLink", () => {
  it("collapsed: renders one icon button with a visible count badge and the i18n label", () => {
    wrap(<AllChatsLink count={7} collapsed />);
    const link = screen.getByRole("link");
    expect(link).toHaveAttribute("href", "/conversations");
    expect(link).toHaveAttribute("aria-label", "All chats (7)");
    expect(
      link.querySelector('[data-slot="sidebar-all-chats-count"]'),
    ).toHaveTextContent("7");
  });

  it("collapsed: hides the count badge at zero (zero-hidden convention)", () => {
    wrap(<AllChatsLink count={0} collapsed />);
    const link = screen.getByRole("link");
    expect(link).toHaveAttribute("aria-label", "All chats");
    expect(
      link.querySelector('[data-slot="sidebar-all-chats-count"]'),
    ).not.toBeInTheDocument();
  });

  it("collapsed: is exactly one chats affordance in the rail (R9-032)", () => {
    const { container } = wrap(<AllChatsLink count={12} collapsed />);
    expect(screen.getAllByRole("link")).toHaveLength(1);
    expect(
      container.querySelectorAll('[data-slot="sidebar-all-chats-collapsed"]'),
    ).toHaveLength(1);
    expect(
      container.querySelectorAll('[data-slot="sidebar-all-chats-count"]'),
    ).toHaveLength(1);
  });

  it("collapsed: the badge updates when the count prop changes (live nav-counts refresh, R9-012)", () => {
    const { rerender } = wrap(<AllChatsLink count={3} collapsed />);
    expect(screen.getByRole("link")).toHaveAttribute(
      "aria-label",
      "All chats (3)",
    );
    rerender(
      <NextIntlClientProvider locale="en" messages={messages}>
        <AllChatsLink count={9} collapsed />
      </NextIntlClientProvider>,
    );
    expect(screen.getByRole("link")).toHaveAttribute(
      "aria-label",
      "All chats (9)",
    );
    expect(
      screen
        .getByRole("link")
        .querySelector('[data-slot="sidebar-all-chats-count"]'),
    ).toHaveTextContent("9");
  });

  it("expanded: renders the plain caption link (byte-identical, no badge markup)", () => {
    wrap(<AllChatsLink count={34} />);
    const link = screen.getByRole("link", { name: "All chats (34)" });
    expect(link).toHaveAttribute("href", "/conversations");
    expect(
      link.querySelector('[data-slot="sidebar-all-chats-count"]'),
    ).not.toBeInTheDocument();
  });
});

describe("MessagesList", () => {
  it("renders persona name as the title line and conversation title as the brief", () => {
    const rows: SidebarConversation[] = [
      {
        id: "c1",
        title: "Rent dispute",
        updated_at: "2026-06-10T00:00:00Z",
        persona: astrid,
      },
    ];
    wrap(<MessagesList conversations={rows} collapsed={false} />);
    expect(screen.getByText("Astrid")).toBeInTheDocument();
    expect(screen.getByText("Rent dispute")).toBeInTheDocument();
    expect(screen.getByRole("link")).toHaveAttribute("href", "/chat/c1");
  });

  it("falls back to untitled brief and unknown-persona title", () => {
    const rows: SidebarConversation[] = [
      {
        id: "c2",
        title: "   ",
        updated_at: "2026-06-10T00:00:00Z",
        persona: null,
      },
    ];
    wrap(<MessagesList conversations={rows} collapsed={false} />);
    expect(screen.getByText("Unknown persona")).toBeInTheDocument();
    expect(screen.getByText("Untitled conversation")).toBeInTheDocument();
  });

  it("marks the active conversation with aria-current", () => {
    pathname = "/chat/c1";
    const rows: SidebarConversation[] = [
      {
        id: "c1",
        title: "Rent dispute",
        updated_at: "2026-06-10T00:00:00Z",
        persona: astrid,
      },
    ];
    wrap(<MessagesList conversations={rows} collapsed={false} />);
    expect(screen.getByRole("link")).toHaveAttribute("aria-current", "page");
    pathname = "/";
  });

  it("renders the empty state when expanded with no conversations", () => {
    wrap(<MessagesList conversations={[]} collapsed={false} />);
    expect(screen.getByText("No conversations yet")).toBeInTheDocument();
  });

  it("renders nothing when collapsed with no conversations", () => {
    const { container } = wrap(<MessagesList conversations={[]} collapsed />);
    expect(container).toBeEmptyDOMElement();
  });

  it("suppresses the visible brief text in collapsed mode", () => {
    const rows: SidebarConversation[] = [
      {
        id: "c1",
        title: "Rent dispute",
        updated_at: "2026-06-10T00:00:00Z",
        persona: astrid,
      },
    ];
    wrap(<MessagesList conversations={rows} collapsed />);
    // The brief is not rendered as text in the rail; the link is avatar-only.
    expect(screen.queryByText("Rent dispute")).not.toBeInTheDocument();
    expect(screen.getByRole("link")).toHaveAttribute("href", "/chat/c1");
  });
});

describe("CallsList", () => {
  const callRow = (over: Partial<SidebarCall> = {}): SidebarCall => ({
    callId: "call_1",
    conversationId: "conv_1",
    startedAt: "2026-06-10T00:00:00Z",
    durationS: 125,
    persona: astrid,
    ...over,
  });

  it("renders persona name + m:ss duration and links the row to the transcript", () => {
    wrap(<CallsList calls={[callRow()]} collapsed={false} />);
    expect(screen.getByText("Astrid")).toBeInTheDocument();
    expect(screen.getByText("2:05")).toBeInTheDocument(); // 125s → 2:05
    // the row links to the SAVED TRANSCRIPT (the chat page renders voice turns).
    expect(screen.getByRole("link")).toHaveAttribute("href", "/chat/conv_1");
  });

  it("falls back to a generic label for a live call (null duration)", () => {
    wrap(
      <CallsList calls={[callRow({ durationS: null })]} collapsed={false} />,
    );
    expect(screen.getByText("Call")).toBeInTheDocument();
  });

  it("falls back to unknown-persona when the persona is missing", () => {
    wrap(<CallsList calls={[callRow({ persona: null })]} collapsed={false} />);
    expect(screen.getByText("Unknown persona")).toBeInTheDocument();
  });

  it("marks the active call row with aria-current", () => {
    pathname = "/chat/conv_1";
    wrap(<CallsList calls={[callRow()]} collapsed={false} />);
    expect(screen.getByRole("link")).toHaveAttribute("aria-current", "page");
    pathname = "/";
  });

  it("renders the empty state when expanded with no calls", () => {
    wrap(<CallsList calls={[]} collapsed={false} />);
    expect(screen.getByText("No calls yet")).toBeInTheDocument();
  });

  it("renders nothing when collapsed with no calls", () => {
    const { container } = wrap(<CallsList calls={[]} collapsed />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("PersonasRail", () => {
  it("links each persona to its page", () => {
    wrap(<PersonasRail personas={[astrid]} collapsed={false} />);
    expect(screen.getByRole("link")).toHaveAttribute(
      "href",
      "/personas/astrid",
    );
  });

  it("renders nothing when there are no personas", () => {
    const { container } = wrap(
      <PersonasRail personas={[]} collapsed={false} />,
    );
    expect(container).toBeEmptyDOMElement();
  });
});
