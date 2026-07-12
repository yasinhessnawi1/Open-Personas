/**
 * Spec F2 T15 — MessageElement tests.
 *
 * Verifies:
 *   1. User vs persona branches render distinctly.
 *   2. D-F1-5 composite on persona path: 2px identity-coloured border-left,
 *      neutral bg-card surface.
 *   3. D-F2-7 once-per-turn rule: avatar renders on first persona message of
 *      a turn; hidden on consecutive persona messages; re-renders after a
 *      user message breaks the turn.
 *   4. Tool-call cards stack above the text content when present.
 *   5. Streaming uses T17 StreamingTextRenderer (delegates correctly).
 *   6. Terminal text uses T11 Markdown (when content is present).
 *   7. TierBadge renders when terminal + tier set; hidden during streaming.
 */

import { render } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it, vi } from "vitest";

// F3 T10 — MessageElement now renders <AuthedImage> for user messages
// with `images`; AuthedImage uses Clerk's useAuth. Stub it so the
// existing test suite doesn't need ClerkProvider boilerplate.
vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ getToken: () => Promise.resolve("test-token") }),
}));

// Spec 35 — user turns now render <UserAvatar>, which reads the account from
// the @/auth façade. Stub it so the suite needs no ClerkProvider. Keep useAuth
// (AuthedImage on image turns reads it from the same façade).
vi.mock("@/auth", () => ({
  useAccount: () => ({
    name: "Tester",
    email: null,
    imageUrl: null,
    available: true,
  }),
  useAuth: () => ({ getToken: () => Promise.resolve("test-token") }),
}));

import {
  MessageElement,
  type MessageElementView,
  type MessageEvent,
} from "./message-element";

const ASTRID = {
  id: "astrid_tenancy_law",
  name: "Astrid",
} as const;

const messages = {
  chat: {
    tierLabel: "{tier} tier",
    toolUsing: "Using {tool}",
    toolError: "error",
    thinking: "{name} is thinking…",
    recalling: "Recalling from {store} memory",
    toolRunning: "{name} is using {tool}…",
    actions: {
      copy: "Copy message",
      copied: "Copied",
      retry: "Retry",
      readAloud: "Read aloud",
      stopReading: "Stop reading",
      loadingAudio: "Loading audio…",
    },
  },
};

function renderWithIntl(node: React.ReactNode) {
  return render(
    <NextIntlClientProvider locale="en" messages={messages}>
      {node}
    </NextIntlClientProvider>,
  );
}

const userMsg = (content: string): MessageElementView => ({
  id: `u-${content}`,
  role: "user",
  content,
});

const personaMsg = (
  content: string,
  extra: Partial<MessageElementView> = {},
): MessageElementView => ({
  id: `a-${content}`,
  role: "assistant",
  content,
  ...extra,
});

describe("MessageElement", () => {
  it("renders user messages right-aligned with bg-secondary", () => {
    const { container } = renderWithIntl(
      <MessageElement message={userMsg("Hello Astrid")} persona={ASTRID} />,
    );
    const wrap = container.querySelector('[data-slot="message-element"]');
    expect(wrap).not.toBeNull();
    expect(wrap?.getAttribute("data-role")).toBe("user");
    expect(wrap?.className).toContain("justify-end");
    // R9-025a: the bubble is targeted by its explicit data-slot (not
    // `firstElementChild`) — it now shares a column with the hover/focus
    // message-action-bar, so its DOM position is no longer index 0.
    const bubble = container.querySelector(
      '[data-slot="message-bubble"]',
    ) as HTMLElement | null;
    expect(bubble?.className).toContain("bg-secondary");
    expect(bubble?.textContent).toBe("Hello Astrid");
  });

  it("renders persona messages with the identity spine — .v-msg__card + byline", () => {
    const { container } = renderWithIntl(
      <MessageElement message={personaMsg("Good morning.")} persona={ASTRID} />,
    );
    const wrap = container.querySelector(
      '[data-slot="message-element"]',
    ) as HTMLElement | null;
    expect(wrap?.getAttribute("data-role")).toBe("persona");
    expect(wrap?.className).toContain("v-msg__persona");
    // The identity colour flows via personaIdentityStyle (--identity-* / --v-id),
    // which the .v-msg__card border-left + .v-id-underline byline consume.
    expect(wrap?.style.getPropertyValue("--identity-h")).not.toBe("");
    expect(container.querySelector(".v-msg__card")).not.toBeNull();
    expect(container.querySelector(".v-msg__byname")).not.toBeNull();
  });

  it("(Spec 35 D-35-4) shows the store-named recall state while streaming pre-content", () => {
    const { container } = renderWithIntl(
      <MessageElement
        message={personaMsg("", {
          streaming: true,
          recall: [
            { store: "self_facts", count: 2 },
            { store: "episodic", count: 3 },
          ],
        })}
        persona={ASTRID}
      />,
    );
    const recall = container.querySelector('[data-slot="recall-state"]');
    expect(recall).not.toBeNull();
    // Names the CURRENT (latest) store + colours its dot via data-store.
    expect(recall?.textContent).toContain("Recalling from episodic memory");
    const dot = container.querySelector(".v-recall-dot");
    expect(dot?.getAttribute("data-store")).toBe("episodic");
  });

  it("(D-F2-7) renders avatar on first persona message (no prevMessage)", () => {
    const { container } = renderWithIntl(
      <MessageElement message={personaMsg("Astrid here.")} persona={ASTRID} />,
    );
    const wrap = container.querySelector('[data-slot="message-element"]');
    expect(wrap?.getAttribute("data-shows-avatar")).toBe("true");
    expect(container.querySelector('[role="img"]')).not.toBeNull();
  });

  it("(D-F2-7) hides avatar on consecutive persona message (same-turn continuation)", () => {
    const first = personaMsg("First reply.");
    const second = personaMsg("Continued reply.");
    const { container } = renderWithIntl(
      <MessageElement message={second} persona={ASTRID} prevMessage={first} />,
    );
    const wrap = container.querySelector('[data-slot="message-element"]');
    expect(wrap?.getAttribute("data-shows-avatar")).toBe("false");
    // The fixed-width avatar slot still occupies space (alignment preserved).
    expect(container.querySelector('[role="img"]')).toBeNull();
  });

  it("(D-F2-7) re-renders avatar after a user message breaks the turn", () => {
    const userBreak = userMsg("Quick question.");
    const personaAfter = personaMsg("Sure — here's my answer.");
    const { container } = renderWithIntl(
      <MessageElement
        message={personaAfter}
        persona={ASTRID}
        prevMessage={userBreak}
      />,
    );
    const wrap = container.querySelector('[data-slot="message-element"]');
    expect(wrap?.getAttribute("data-shows-avatar")).toBe("true");
    expect(container.querySelector('[role="img"]')).not.toBeNull();
  });

  it("stacks tool-call cards above the text content", () => {
    const msg = personaMsg("Found it.", {
      tools: [
        {
          toolName: "web_search",
          args: { q: "husleieloven §5-7" },
          result: "Found.",
          pending: false,
        },
      ],
    });
    const { container } = renderWithIntl(
      <MessageElement message={msg} persona={ASTRID} />,
    );
    const body = container.querySelector('[data-slot="message-element-body"]');
    // Tool-call card is rendered as a child of the body — verify presence.
    expect(body?.textContent).toContain("web_search");
  });

  it("delegates to StreamingTextRenderer when message.streaming=true", () => {
    const msg = personaMsg("Astrid is thi", { streaming: true });
    const { container } = renderWithIntl(
      <MessageElement message={msg} persona={ASTRID} />,
    );
    // Streaming renderer mounts → its data-slot is present.
    expect(
      container.querySelector('[data-slot="streaming-text"]'),
    ).not.toBeNull();
    // The Markdown content slot is NOT mounted during streaming.
    expect(
      container.querySelector('[data-slot="message-element-content"]'),
    ).toBeNull();
  });

  it("uses Markdown for terminal text (non-streaming, content present)", () => {
    const msg = personaMsg("**Done.** Here is the answer.");
    const { container } = renderWithIntl(
      <MessageElement message={msg} persona={ASTRID} />,
    );
    const contentSlot = container.querySelector(
      '[data-slot="message-element-content"]',
    );
    expect(contentSlot).not.toBeNull();
    // Markdown renders **Done.** as <strong>.
    expect(contentSlot?.querySelector("strong")?.textContent).toBe("Done.");
  });

  it("shows TierBadge when terminal + tier set", () => {
    const msg = personaMsg("Final.", { tier: "frontier" });
    const { container } = renderWithIntl(
      <MessageElement message={msg} persona={ASTRID} />,
    );
    expect(container.querySelector('[data-slot="tier-badge"]')).not.toBeNull();
  });

  it("hides TierBadge during streaming even when tier is set", () => {
    const msg = personaMsg("Streaming...", {
      streaming: true,
      tier: "frontier",
    });
    const { container } = renderWithIntl(
      <MessageElement message={msg} persona={ASTRID} />,
    );
    expect(container.querySelector('[data-slot="tier-badge"]')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// D-F2-15 interleaved layout (T26 amendment, 2026-06-06)

const interleavedMsg = (
  events: MessageEvent[],
  extra: Partial<MessageElementView> = {},
): MessageElementView => ({
  id: `i-${events.length}`,
  role: "assistant",
  content: events
    .filter(
      (e): e is Extract<MessageEvent, { kind: "text" }> => e.kind === "text",
    )
    .map((e) => e.delta)
    .join(""),
  events,
  ...extra,
});

describe("MessageElement — D-F2-15 interleaved layout", () => {
  it("flips data-layout to 'interleaved' when events[] is present", () => {
    const msg = interleavedMsg([{ kind: "text", delta: "Hi." }]);
    const { container } = renderWithIntl(
      <MessageElement message={msg} persona={ASTRID} />,
    );
    const wrap = container.querySelector('[data-slot="message-element"]');
    expect(wrap?.getAttribute("data-layout")).toBe("interleaved");
  });

  it("renders text + tool cards in stream order (not stacked)", () => {
    const msg = interleavedMsg([
      { kind: "text", delta: "Let me search. " },
      {
        kind: "tool_call",
        callId: "c0",
        toolName: "web_search",
        args: { q: "husleieloven" },
      },
      {
        kind: "tool_result",
        toolName: "web_search",
        content: "Section 5-1 explains it.",
        isError: false,
      },
      { kind: "text", delta: "Got it." },
    ]);
    const { container } = renderWithIntl(
      <MessageElement message={msg} persona={ASTRID} />,
    );
    const interleaved = container.querySelector(
      '[data-slot="message-element-interleaved"]',
    );
    expect(interleaved).not.toBeNull();
    const children = Array.from(interleaved?.children ?? []);
    // text span → tool card → text span (3 items, in stream order).
    expect(children.length).toBe(3);
    expect(children[0]?.getAttribute("data-slot")).toBe("message-event-text");
    expect(children[0]?.textContent).toContain("Let me search");
    expect(children[2]?.getAttribute("data-slot")).toBe("message-event-text");
    expect(children[2]?.textContent).toContain("Got it");
  });

  it("pairs tool_call with the next matching tool_result (FIFO by toolName)", () => {
    // ToolCallCard's body is in a <Collapsible>, closed by default, so result
    // content isn't in the DOM until expanded. Instead: verify both cards
    // render, the first marks success (no error indicator), and the second
    // marks error (· error appended from the tool_result's isError flag).
    const msg = interleavedMsg([
      {
        kind: "tool_call",
        callId: "c0",
        toolName: "web_search",
        args: { q: "a" },
      },
      {
        kind: "tool_result",
        toolName: "web_search",
        content: "Result A",
        isError: false,
      },
      {
        kind: "tool_call",
        callId: "c1",
        toolName: "web_search",
        args: { q: "b" },
      },
      {
        kind: "tool_result",
        toolName: "web_search",
        content: "Result B",
        isError: true,
      },
    ]);
    const { container } = renderWithIntl(
      <MessageElement message={msg} persona={ASTRID} />,
    );
    const interleaved = container.querySelector(
      '[data-slot="message-element-interleaved"]',
    );
    // Count occurrences of the "Using web_search" trigger text — one per
    // tool card. Result content lives inside <Collapsible>, closed by
    // default, so it's not in the rendered textContent until expanded.
    const usingMatches = (interleaved?.textContent ?? "").match(
      /Using web_search/g,
    );
    expect(usingMatches?.length).toBe(2);
    // The second card's tool_result was an error → "· error" appears once.
    const errorMatches = (interleaved?.textContent ?? "").match(/· error/g);
    expect(errorMatches?.length).toBe(1);
  });

  it("shows the thinking indicator when streaming and events is empty", () => {
    const msg = interleavedMsg([], { streaming: true });
    const { container } = renderWithIntl(
      <MessageElement message={msg} persona={ASTRID} />,
    );
    expect(
      container.querySelector('[data-slot="streaming-thinking"]'),
    ).not.toBeNull();
  });

  it("shows the tool-running indicator when streaming and a tool_call has no result", () => {
    const msg = interleavedMsg(
      [
        { kind: "text", delta: "Searching… " },
        {
          kind: "tool_call",
          callId: "c0",
          toolName: "web_search",
          args: { q: "x" },
        },
      ],
      { streaming: true },
    );
    const { container } = renderWithIntl(
      <MessageElement message={msg} persona={ASTRID} />,
    );
    const running = container.querySelector(
      '[data-slot="message-element-tool-running"]',
    );
    expect(running).not.toBeNull();
    expect(running?.getAttribute("aria-label")).toContain("Astrid");
    expect(running?.getAttribute("aria-label")).toContain("web_search");
  });

  it("hides the tool-running indicator once a matching tool_result arrives", () => {
    const msg = interleavedMsg(
      [
        {
          kind: "tool_call",
          callId: "c0",
          toolName: "web_search",
          args: { q: "x" },
        },
        {
          kind: "tool_result",
          toolName: "web_search",
          content: "ok",
          isError: false,
        },
      ],
      { streaming: true },
    );
    const { container } = renderWithIntl(
      <MessageElement message={msg} persona={ASTRID} />,
    );
    expect(
      container.querySelector('[data-slot="message-element-tool-running"]'),
    ).toBeNull();
  });

  it("shows the working pulse in the gap between rounds (streaming + working, completed tool, no pending)", () => {
    // After a tool_result, the model writes the next code_execution call (no
    // text/tool event yet). The `thinking` event sets working=true → the gap
    // shows the thinking pulse instead of looking frozen.
    const msg = interleavedMsg(
      [
        { kind: "text", delta: "Let me build it. " },
        {
          kind: "tool_call",
          callId: "c0",
          toolName: "code_execution",
          args: {},
        },
        {
          kind: "tool_result",
          toolName: "code_execution",
          content: "ok",
          isError: false,
        },
      ],
      { streaming: true, working: true },
    );
    const { container } = renderWithIntl(
      <MessageElement message={msg} persona={ASTRID} />,
    );
    expect(
      container.querySelector('[data-slot="streaming-thinking"]'),
    ).not.toBeNull();
  });

  it("does NOT show the working pulse once a tool is pending (tool-running wins)", () => {
    const msg = interleavedMsg(
      [
        {
          kind: "tool_call",
          callId: "c0",
          toolName: "code_execution",
          args: {},
        },
        // no tool_result → pending
      ],
      { streaming: true, working: true },
    );
    const { container } = renderWithIntl(
      <MessageElement message={msg} persona={ASTRID} />,
    );
    // The pending tool's running indicator shows; the gap pulse does not duplicate it.
    expect(
      container.querySelector('[data-slot="message-element-tool-running"]'),
    ).not.toBeNull();
    expect(
      container.querySelector('[data-slot="streaming-thinking"]'),
    ).toBeNull();
  });

  it("does NOT show the working pulse on a terminal (non-streaming) turn", () => {
    const msg = interleavedMsg(
      [
        {
          kind: "tool_call",
          callId: "c0",
          toolName: "code_execution",
          args: {},
        },
        {
          kind: "tool_result",
          toolName: "code_execution",
          content: "ok",
          isError: false,
        },
      ],
      { streaming: false, working: true },
    );
    const { container } = renderWithIntl(
      <MessageElement message={msg} persona={ASTRID} />,
    );
    expect(
      container.querySelector('[data-slot="streaming-thinking"]'),
    ).toBeNull();
  });

  it("shows the caret next to the last text span when streaming and last event was text", () => {
    const msg = interleavedMsg(
      [
        {
          kind: "tool_call",
          callId: "c0",
          toolName: "web_search",
          args: { q: "x" },
        },
        {
          kind: "tool_result",
          toolName: "web_search",
          content: "ok",
          isError: false,
        },
        { kind: "text", delta: "I found it." },
      ],
      { streaming: true },
    );
    const { container } = renderWithIntl(
      <MessageElement message={msg} persona={ASTRID} />,
    );
    expect(
      container.querySelector('[data-slot="message-element-caret"]'),
    ).not.toBeNull();
  });

  it("hides the caret when streaming has ended", () => {
    const msg = interleavedMsg([{ kind: "text", delta: "Done." }], {
      streaming: false,
    });
    const { container } = renderWithIntl(
      <MessageElement message={msg} persona={ASTRID} />,
    );
    expect(
      container.querySelector('[data-slot="message-element-caret"]'),
    ).toBeNull();
  });

  it("falls back to the stacked layout when events[] is absent (back-compat)", () => {
    const msg = personaMsg("No events here.");
    const { container } = renderWithIntl(
      <MessageElement message={msg} persona={ASTRID} />,
    );
    const wrap = container.querySelector('[data-slot="message-element"]');
    expect(wrap?.getAttribute("data-layout")).toBe("stacked");
  });
});

describe("MessageElement — F3 T10 attached images on user messages", () => {
  it("renders attached images inside the user bubble via AuthedImage", () => {
    const msg: MessageElementView = {
      id: "u-1",
      role: "user",
      content: "What is this?",
      images: [
        { workspace_path: "uploads/a.png", media_type: "image/png" },
        { workspace_path: "uploads/b.jpeg", media_type: "image/jpeg" },
      ],
    };
    const { container } = renderWithIntl(
      <MessageElement message={msg} persona={ASTRID} />,
    );
    const wrap = container.querySelector('[data-slot="message-element"]');
    expect(wrap?.getAttribute("data-role")).toBe("user");
    const imagesBlock = container.querySelector('[data-slot="message-images"]');
    expect(imagesBlock).not.toBeNull();
    expect(imagesBlock?.getAttribute("data-count")).toBe("2");
    // The text content stays present too — it's the "look at this AND tell me…" flow.
    expect(wrap?.textContent).toContain("What is this?");
  });

  it("renders text-only path byte-for-byte when images is absent (regression guard)", () => {
    const msg: MessageElementView = {
      id: "u-2",
      role: "user",
      content: "Just text.",
      // images intentionally absent
    };
    const { container } = renderWithIntl(
      <MessageElement message={msg} persona={ASTRID} />,
    );
    expect(container.querySelector('[data-slot="message-images"]')).toBeNull();
    expect(container.textContent).toContain("Just text.");
  });

  it("renders text-only path byte-for-byte when images is empty array", () => {
    const msg: MessageElementView = {
      id: "u-3",
      role: "user",
      content: "Empty array case.",
      images: [],
    };
    const { container } = renderWithIntl(
      <MessageElement message={msg} persona={ASTRID} />,
    );
    expect(container.querySelector('[data-slot="message-images"]')).toBeNull();
  });
});
