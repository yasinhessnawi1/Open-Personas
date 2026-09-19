/**
 * Leaked tool-call markup must never render, live or from a stored record.
 *
 * The runtime now strips it at the provider boundary, so new messages arrive
 * clean. These tests cover the messages already written to the database before
 * that fix: reopening one of those conversations must not show the markup
 * either (R9-157: live and reopened agree).
 */

import { render } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it, vi } from "vitest";

vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ getToken: () => Promise.resolve("test-token") }),
}));
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

const ASTRID = { id: "astrid_tenancy_law", name: "Astrid" } as const;

const messages = {
  chat: {
    tierLabel: "{tier} tier",
    originatedBadge: "Started this",
    originatedLabel: "{name} sent this without being asked",
    toolUsing: "Using {tool}",
    toolError: "error",
    thinking: "{name} is thinking…",
    recalling: "Recalling from {store} memory",
    toolRunning: "{name} is using {tool}…",
    actions: {
      copy: "Copy message",
      copied: "Copied",
      retry: "Retry",
      edit: "Edit message",
      editSave: "Save & rerun",
      editCancel: "Cancel",
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

// Verbatim from production (GitHub issues #10 and #13).
const LEAK_PAREN =
  "Let me check what's actually on the books.\n" +
  '<tool_call>schedule_introspect(scope: "all", days_ahead: 7)' +
  "The schedule is in good order:";
const LEAK_MANGLED =
  'Let me check the clock so we know exactly when "an hour from now" is.' +
  '<tool_call>datetime tool_call: </arg_value><arg_key>tool": "mcp_search", ' +
  '"args": {"query": "schedule a one-time task", "top_k": 5}}';

const MARKUP = [
  "<tool_call>",
  "<arg_key>",
  "<arg_value>",
  "</arg_value>",
  "</tool_call>",
];

function assertNoMarkup(container: HTMLElement) {
  const text = container.textContent ?? "";
  for (const marker of MARKUP) {
    expect(text).not.toContain(marker);
  }
}

const personaMsg = (
  content: string,
  extra: Partial<MessageElementView> = {},
): MessageElementView => ({
  id: "a-leak",
  role: "assistant",
  content,
  ...extra,
});

describe("MessageElement, leaked tool-call markup", () => {
  it("renders none of it from a stored record (reopened, stacked)", () => {
    const { container } = renderWithIntl(
      <MessageElement message={personaMsg(LEAK_PAREN)} persona={ASTRID} />,
    );
    assertNoMarkup(container);
    expect(container.textContent).toContain("Let me check what's actually");
    expect(container.textContent).toContain("The schedule is in good order:");
  });

  it("renders none of it live (streaming, stacked)", () => {
    const { container } = renderWithIntl(
      <MessageElement
        message={personaMsg(LEAK_MANGLED, { streaming: true })}
        persona={ASTRID}
      />,
    );
    assertNoMarkup(container);
    expect(container.textContent).not.toContain("mcp_search");
    expect(container.textContent).toContain('when "an hour from now" is.');
  });

  it("renders none of it from a stored event log (reopened, interleaved)", () => {
    const events: MessageEvent[] = [
      { kind: "text", delta: "Let me check.\n" },
      { kind: "text", delta: '<tool_call>schedule_introspect(scope: "all")' },
      { kind: "tool_call", callId: "c1", toolName: "schedule_introspect" },
      {
        kind: "tool_result",
        toolName: "schedule_introspect",
        content: "one entry",
        isError: false,
      },
      { kind: "text", delta: "The schedule is in good order:" },
    ];
    const { container } = renderWithIntl(
      <MessageElement
        message={personaMsg(LEAK_PAREN, { events })}
        persona={ASTRID}
      />,
    );
    assertNoMarkup(container);
    expect(container.textContent).toContain("Let me check.");
    expect(container.textContent).toContain("The schedule is in good order:");
    // The real call still renders its card. The leak guard hides markup, not tools.
    expect(container.textContent).toContain("Using schedule_introspect");
  });

  it("renders none of it live from a growing event log (interleaved)", () => {
    const events: MessageEvent[] = [
      { kind: "text", delta: "Let me check.\n" },
      { kind: "text", delta: "<tool_c" },
    ];
    const { container } = renderWithIntl(
      <MessageElement
        message={personaMsg("Let me check.\n<tool_c", {
          events,
          streaming: true,
        })}
        persona={ASTRID}
      />,
    );
    assertNoMarkup(container);
    expect(container.textContent).not.toContain("<tool_c");
    expect(container.textContent).toContain("Let me check.");
  });
});
