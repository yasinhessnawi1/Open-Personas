/**
 * Spec 35 — ConversationFiles (the unified chat Files viewer).
 *
 * Verifies:
 *   1. The header button shows a count badge of the conversation's files.
 *   2. Opening the panel groups the unified list by provenance
 *      (uploads → "Shared by you", generated → "Made by {persona}").
 *   3. Selecting a file drives the reused <ArtifactView> with its ref.
 *   4. Empty conversation → no badge + the empty state.
 */

import { fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it, vi } from "vitest";
import type { ArtifactItem } from "@/lib/hooks/use-conversation-artifacts";

// Auth façade — the download path + provider read it; stub to avoid Clerk.
vi.mock("@/auth", () => ({
  useAuth: () => ({ getToken: () => Promise.resolve("test-token") }),
}));

// The reused Spec-28 renderer — stub it to surface the workspacePath it renders
// so we can assert selection wiring without the byte-loading machinery.
vi.mock("./renderers", () => ({
  ArtifactView: ({ workspacePath }: { workspacePath: string }) => (
    <div data-testid="artifact-view-stub">{workspacePath}</div>
  ),
}));

// Drive the component off a controllable artifact list.
const items: ArtifactItem[] = [];
vi.mock("@/lib/hooks/use-conversation-artifacts", () => ({
  CONVERSATION_FILES_CHANGED_EVENT: "conversation-files-changed",
  notifyConversationFilesChanged: () => {},
  useConversationArtifacts: () => ({
    items,
    loading: false,
    error: null,
    refresh: () => Promise.resolve(),
  }),
}));

import { ConversationFiles } from "./conversation-files";

const messages = {
  chat: {
    files: {
      button: "Files",
      title: "Files in this conversation",
      uploaded: "Shared by you",
      generated: "Made by {name}",
      empty: "No files yet",
      emptyHint: "Files you upload and files {name} creates appear here.",
      downloadShort: "Download",
      selectPrompt: "Select a file to preview",
    },
    output: {
      renderer: { showRendered: "Show rendered view", showRaw: "Show source" },
    },
  },
};

function setItems(next: ArtifactItem[]) {
  items.length = 0;
  items.push(...next);
}

function renderFiles() {
  return render(
    <NextIntlClientProvider locale="en" messages={messages}>
      <ConversationFiles
        personaId="mara"
        conversationId="conv_1"
        personaName="Mara"
      />
    </NextIntlClientProvider>,
  );
}

const upload: ArtifactItem = {
  ref: "uploads/husleie.pdf",
  size_bytes: 2048,
  media_type: "application/pdf",
  metadata: {
    source: "upload",
    type: "doc",
    producing_spec: "14",
    conversation_id: "conv_1",
    created_at: "2026-06-19T00:00:00Z",
    original_name: "husleie.pdf",
  },
};
const generated: ArtifactItem = {
  ref: "out/pricing.png",
  size_bytes: 4096,
  media_type: "image/png",
  metadata: {
    source: "generated",
    type: "chart",
    producing_spec: "15",
    conversation_id: "conv_1",
    created_at: "2026-06-19T00:00:00Z",
    original_name: "pricing.png",
  },
};

describe("ConversationFiles", () => {
  it("badges the button with the conversation's file count", () => {
    setItems([upload, generated]);
    const { container } = renderFiles();
    const badge = container.querySelector(
      '[data-slot="conversation-files-count"]',
    );
    expect(badge?.textContent).toBe("2");
  });

  it("opens a provenance-grouped list and previews the selection", () => {
    setItems([upload, generated]);
    renderFiles();
    fireEvent.click(screen.getByRole("button", { name: "Files" }));

    expect(screen.getByText("Shared by you")).toBeInTheDocument();
    expect(screen.getByText("Made by Mara")).toBeInTheDocument();
    // husleie is auto-selected (first item) → appears in both rail + toolbar.
    expect(screen.getAllByText("husleie.pdf").length).toBeGreaterThan(0);

    // The generated chart row selects → ArtifactView stub renders its ref.
    fireEvent.click(screen.getByText("pricing.png"));
    expect(screen.getByTestId("artifact-view-stub").textContent).toBe(
      "out/pricing.png",
    );
  });

  it("shows the empty state and no badge when the conversation has no files", () => {
    setItems([]);
    const { container } = renderFiles();
    expect(
      container.querySelector('[data-slot="conversation-files-count"]'),
    ).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Files" }));
    expect(screen.getByText("No files yet")).toBeInTheDocument();
  });

  // R9-024: the controlled open/onOpenChange escape hatch — lets a sibling
  // right panel (the new chat Calendar) close this one, "one panel at a time".
  describe("controlled open (R9-024)", () => {
    it("open=false renders the button but never auto-opens the panel", () => {
      setItems([]);
      render(
        <NextIntlClientProvider locale="en" messages={messages}>
          <ConversationFiles
            personaId="mara"
            conversationId="conv_1"
            personaName="Mara"
            open={false}
            onOpenChange={() => {}}
          />
        </NextIntlClientProvider>,
      );
      expect(screen.queryByText("Files in this conversation")).toBeNull();
    });

    it("open=true renders the panel without a button click", () => {
      setItems([]);
      render(
        <NextIntlClientProvider locale="en" messages={messages}>
          <ConversationFiles
            personaId="mara"
            conversationId="conv_1"
            personaName="Mara"
            open
            onOpenChange={() => {}}
          />
        </NextIntlClientProvider>,
      );
      expect(
        screen.getByText("Files in this conversation"),
      ).toBeInTheDocument();
    });

    it("clicking the button calls onOpenChange(true) instead of managing its own state", () => {
      setItems([]);
      const onOpenChange = vi.fn();
      render(
        <NextIntlClientProvider locale="en" messages={messages}>
          <ConversationFiles
            personaId="mara"
            conversationId="conv_1"
            personaName="Mara"
            open={false}
            onOpenChange={onOpenChange}
          />
        </NextIntlClientProvider>,
      );
      fireEvent.click(screen.getByRole("button", { name: "Files" }));
      expect(onOpenChange).toHaveBeenCalledWith(true);
      // Controlled: the panel does NOT open on its own (the parent didn't flip the prop).
      expect(screen.queryByText("Files in this conversation")).toBeNull();
    });
  });
});
