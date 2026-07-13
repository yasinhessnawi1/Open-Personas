/**
 * R9-028 — the calls list: title leads (R9-020's title_refresh voice leg),
 * persona name + time + duration trail as the secondary line — the same
 * title-primary row shape `<ConversationList>` uses. Plus the R9-028 (c)
 * client-side `?persona_id=` / `?q=` filtering (no new API surface).
 */
import { render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  type CallHistoryItem,
  CallHistoryList,
  type CallHistoryPersona,
} from "./call-history-list";

let searchString = "";
vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(searchString),
}));

const messages = {
  calls: {
    untitled: "Untitled call",
    unknownPersona: "Unknown persona",
    noMatches: "No calls match the current filters.",
  },
};

const PERSONAS: Record<string, CallHistoryPersona> = {
  astrid: { id: "astrid", name: "Astrid Berg", avatar_url: null },
  lena: { id: "lena", name: "Lena Brevik", avatar_url: null },
};

const CALLS: CallHistoryItem[] = [
  {
    call_id: "call_1",
    conversation_id: "conv_1",
    persona_id: "astrid",
    title: "Bergen trip planning",
    started_at: "2026-07-01T10:00:00Z",
    duration_s: 125,
  },
  {
    call_id: "call_2",
    conversation_id: "conv_2",
    persona_id: "lena",
    title: "",
    started_at: "2026-07-02T10:00:00Z",
    duration_s: null,
  },
];

function renderList(calls: CallHistoryItem[] = CALLS) {
  return render(
    <NextIntlClientProvider locale="en" messages={messages}>
      <CallHistoryList calls={calls} personaById={PERSONAS} />
    </NextIntlClientProvider>,
  );
}

describe("CallHistoryList — title leads, persona trails (R9-028)", () => {
  beforeEach(() => {
    searchString = "";
  });

  it("renders the call's title as the primary line and the persona name in the meta line", () => {
    renderList();
    const row = screen.getByText("Bergen trip planning").closest("li");
    expect(row).not.toBeNull();
    expect(row?.textContent).toContain("Astrid Berg");
  });

  it("falls back to the untitled label when the call has no title yet", () => {
    renderList();
    const row = screen.getByText("Untitled call").closest("li");
    expect(row?.textContent).toContain("Lena Brevik");
  });

  it("falls back to the unknown-persona label when the persona can't be resolved", () => {
    renderList([
      {
        call_id: "call_3",
        conversation_id: "conv_3",
        persona_id: "ghost",
        title: "Mystery call",
        started_at: "2026-07-03T10:00:00Z",
        duration_s: null,
      },
    ]);
    expect(screen.getByText("Mystery call")).toBeInTheDocument();
    expect(screen.getByText(/Unknown persona/)).toBeInTheDocument();
  });
});

describe("CallHistoryList — R9-028 (c) client-side filtering", () => {
  beforeEach(() => {
    searchString = "";
  });

  it("filters by ?persona_id=", () => {
    searchString = "persona_id=lena";
    renderList();
    expect(screen.queryByText("Bergen trip planning")).toBeNull();
    expect(screen.getByText("Untitled call")).toBeInTheDocument();
  });

  it("filters by ?q= (case-insensitive title substring)", () => {
    searchString = "q=BERGEN";
    renderList();
    expect(screen.getByText("Bergen trip planning")).toBeInTheDocument();
    expect(screen.queryByText("Untitled call")).toBeNull();
  });

  it("shows the no-matches copy when the filters exclude every call", () => {
    searchString = "q=nonexistent";
    renderList();
    expect(
      screen.getByText("No calls match the current filters."),
    ).toBeInTheDocument();
  });
});
