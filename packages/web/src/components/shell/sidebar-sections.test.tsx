/**
 * Structural tests for the app-sidebar section bodies.
 *
 * Asserts the chat-app MESSAGES contract (title = persona name, brief =
 * conversation title, untitled/unknown fallbacks, active row, empty state,
 * collapsed-mode label suppression) and the PERSONAS rail links.
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
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

// R9-036: PersonasRail's avatars are now gesture-capable — each one drives
// useApi / useCallSession / useRouter / the startChat server action (the
// SAME seams persona-library-card.tsx / new-call-button.tsx / R9-014's
// new-conversation-button.tsx already use). Mocked with the identical
// vi.hoisted pattern persona-library-card.test.tsx already established.
const h = vi.hoisted(() => ({
  push: vi.fn(),
  requestCall: vi.fn(() => "started" as "started" | "current" | "switch"),
  post: vi.fn(async () => ({ data: { id: "new-conv" } })),
  startChat: vi.fn(),
}));

let pathname = "/";
vi.mock("next/navigation", () => ({
  usePathname: () => pathname,
  useRouter: () => ({ push: h.push, refresh: vi.fn() }),
}));
vi.mock("@/app/actions", () => ({
  startChat: (id: string) => h.startChat(id),
}));
vi.mock("@/lib/api/use-api", () => ({
  useApi: () => ({ POST: h.post, GET: vi.fn(), DELETE: vi.fn() }),
}));
vi.mock("@/lib/voice/call-session-context", () => ({
  useCallSession: () => ({ requestCall: h.requestCall }),
}));

beforeEach(() => {
  h.push.mockClear();
  h.requestCall.mockReset();
  h.requestCall.mockReturnValue("started");
  h.post.mockClear();
  h.startChat.mockClear();
});

const messages = {
  nav: {
    sidebar: {
      messagesEmpty: "No conversations yet",
      allChats: "{count, plural, =0 {All chats} other {All chats (#)}}",
      untitled: "Untitled conversation",
      unknownPersona: "Unknown persona",
      callsEmpty: "No calls yet",
      callOngoing: "Call",
      callPersona: "Call {name}",
      newChat: "New chat",
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

  // R9-036 REOPEN — real swipe, no hold. The gesture MECHANICS (tap-slop,
  // circle-radius geometry, lock/unlock, cancel, tap-through) are
  // exhaustively covered at the hook level
  // (lib/hooks/use-press-swipe-gesture.test.tsx); these prove the
  // SIDEBAR-SPECIFIC WIRING — that a committed "up" actually drives the call
  // origination seam and a committed "down" actually calls `startChat` — in
  // both collapsed and expanded mounts.
  //
  // Neither PersonaRailItem's Link nor jsdom stub `getBoundingClientRect`
  // here, so the hook measures an all-zero rect and falls back to its
  // documented default radius (20px — DEFAULT_RADIUS_FALLBACK_PX, matching
  // the avatar's own real "md" size). Pressing at clientY:0, dy IS the
  // distance from both the press position and the (zero) center, so a
  // ±40px move is comfortably past the 8px tap-slop AND the 20px radius.
  describe("swipe gesture wiring", () => {
    const POINTER_ID = 1;

    beforeEach(() => {
      vi.useFakeTimers();
    });

    afterEach(() => {
      vi.useRealTimers();
    });

    // Fake timers + a mocked-async `api.POST` (a real Promise, resolved on
    // the microtask queue) combine safely via `advanceTimersByTimeAsync`,
    // which flushes microtasks after the synchronous swipe — unlike
    // `vi.waitFor`'s real-time polling, which fake timers would otherwise
    // starve. `swipe` is async for exactly this reason (there's no hold to
    // wait out anymore — the whole gesture is one pointerdown/move/up).
    async function swipe(target: Element, clientY: number) {
      fireEvent.pointerDown(target, {
        clientX: 0,
        clientY: 0,
        pointerId: POINTER_ID,
        pointerType: "touch",
        button: 0,
      });
      fireEvent.pointerMove(document, {
        clientX: 0,
        clientY,
        pointerId: POINTER_ID,
      });
      fireEvent.pointerUp(document, {
        clientX: 0,
        clientY,
        pointerId: POINTER_ID,
      });
      // Flush the commit callback's promise chain (api.POST → requestCall →
      // router.push, or the mocked startChat call).
      await vi.advanceTimersByTimeAsync(0);
    }

    it.each([[false], [true]])(
      "collapsed=%s: swipe UP past the circle's radius and release calls the persona (mints a call-origin conversation → requestCall → navigate)",
      async (collapsed) => {
        wrap(<PersonasRail personas={[astrid]} collapsed={collapsed} />);
        await swipe(screen.getByRole("link"), -40); // past slop and the 20px fallback radius, upward
        expect(h.post).toHaveBeenCalledTimes(1);
        expect(h.post).toHaveBeenCalledWith(
          "/v1/personas/{persona_id}/conversations",
          expect.objectContaining({
            params: { path: { persona_id: "astrid" } },
            body: { title: "", origin: "call" },
          }),
        );
        expect(h.requestCall).toHaveBeenCalledWith(
          expect.objectContaining({
            personaId: "astrid",
            conversationId: "new-conv",
            personaName: "Astrid",
          }),
        );
        expect(h.push).toHaveBeenCalledWith("/chat/new-conv/voice");
        expect(h.startChat).not.toHaveBeenCalled();
      },
    );

    it.each([[false], [true]])(
      "collapsed=%s: swipe DOWN past the circle's radius and release starts a chat via startChat",
      async (collapsed) => {
        wrap(<PersonasRail personas={[astrid]} collapsed={collapsed} />);
        await swipe(screen.getByRole("link"), 40); // past slop and the 20px fallback radius, downward
        expect(h.startChat).toHaveBeenCalledWith("astrid");
        expect(h.post).not.toHaveBeenCalled();
        expect(h.requestCall).not.toHaveBeenCalled();
      },
    );

    it.each([[false], [true]])(
      "collapsed=%s: swiping past slop but releasing before the circle's radius cancels — neither seam fires",
      async (collapsed) => {
        wrap(<PersonasRail personas={[astrid]} collapsed={collapsed} />);
        await swipe(screen.getByRole("link"), -12); // past the 8px slop, under the 20px fallback radius
        expect(h.post).not.toHaveBeenCalled();
        expect(h.startChat).not.toHaveBeenCalled();
      },
    );

    it("the browser's post-release click is suppressed after a commit — onNavigate (e.g. closing the mobile sheet) does NOT also fire", async () => {
      // jsdom, unlike a real browser, does not auto-synthesize a `click`
      // after a pointerdown/pointerup pair — fire it explicitly, the same
      // way the hook-level test does, to model what a real tap-release
      // triggers natively. `onNavigate` is the one sidebar-visible effect a
      // NORMAL click has (closing the mobile sheet) that a swiped gesture's
      // synthesized click must NOT also trigger.
      const onNavigate = vi.fn();
      wrap(
        <PersonasRail
          personas={[astrid]}
          collapsed={false}
          onNavigate={onNavigate}
        />,
      );
      const link = screen.getByRole("link");
      await swipe(link, 40); // commits "down" (startChat)
      expect(h.startChat).toHaveBeenCalledTimes(1);
      fireEvent.click(link);
      expect(onNavigate).not.toHaveBeenCalled();
      // Still exactly one action — the commit's own click was suppressed,
      // not merely un-observed.
      expect(h.startChat).toHaveBeenCalledTimes(1);
      expect(h.post).not.toHaveBeenCalled();
    });

    it("a plain click (with onNavigate wired) still fires onNavigate — the gesture doesn't over-suppress ordinary taps", () => {
      const onNavigate = vi.fn();
      wrap(
        <PersonasRail
          personas={[astrid]}
          collapsed={false}
          onNavigate={onNavigate}
        />,
      );
      fireEvent.click(screen.getByRole("link"));
      expect(onNavigate).toHaveBeenCalledTimes(1);
      expect(h.post).not.toHaveBeenCalled();
      expect(h.startChat).not.toHaveBeenCalled();
    });

    it("call entry does NOT navigate when a switch confirm is pending (inherits the one-call rule)", async () => {
      h.requestCall.mockReturnValue("switch");
      wrap(<PersonasRail personas={[astrid]} collapsed={false} />);
      await swipe(screen.getByRole("link"), -40);
      expect(h.requestCall).toHaveBeenCalled();
      expect(h.push).not.toHaveBeenCalled();
    });
  });
});
