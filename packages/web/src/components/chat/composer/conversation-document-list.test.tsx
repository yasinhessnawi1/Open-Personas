import { fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it, vi } from "vitest";
import en from "@/i18n/messages/en.json";
import type { DocumentRef } from "@/lib/upload";
import { ConversationDocumentList } from "./conversation-document-list";

function renderWithIntl(ui: React.ReactNode) {
  return render(
    <NextIntlClientProvider locale="en" messages={en}>
      {ui}
    </NextIntlClientProvider>,
  );
}

function doc(overrides: Partial<DocumentRef> = {}): DocumentRef {
  return {
    doc_ref: "d-1",
    filename: "a.pdf",
    title: "a.pdf",
    format: "pdf",
    workspace_path: "p/c/d/a.pdf",
    strategy: "whole_inject" as const,
    token_count: 100,
    page_count: 1,
    sheet_names: null,
    size_bytes: 1000,
    images: [],
    text_stored: false,
    text_partial: false,
    index_state: "not_needed" as const,
    chunks_indexed: null,
    ...overrides,
  };
}

describe("<ConversationDocumentList>", () => {
  it("renders nothing when documents is empty (clean composer surface)", () => {
    const { container } = renderWithIntl(
      <ConversationDocumentList documents={[]} onRemove={vi.fn()} />,
    );
    expect(
      container.querySelector("[data-slot='conversation-document-list']"),
    ).toBeNull();
  });

  it("renders one DocumentChip per document with data-count attribute", () => {
    const { container } = renderWithIntl(
      <ConversationDocumentList
        documents={[doc({ doc_ref: "d-1" }), doc({ doc_ref: "d-2" })]}
        onRemove={vi.fn()}
      />,
    );
    const panel = container.querySelector(
      "[data-slot='conversation-document-list']",
    );
    expect(panel).not.toBeNull();
    expect(panel?.getAttribute("data-count")).toBe("2");
    const chips = container.querySelectorAll("[data-slot='document-chip']");
    expect(chips.length).toBe(2);
  });

  it("propagates onRemove with the chip's docRef", () => {
    const onRemove = vi.fn();
    renderWithIntl(
      <ConversationDocumentList documents={[doc()]} onRemove={onRemove} />,
    );
    fireEvent.click(screen.getByLabelText(en.chat.composer.attach.remove));
    expect(onRemove).toHaveBeenCalledWith("d-1");
  });

  it("uses i18n panel title (T20 a11y discipline)", () => {
    renderWithIntl(
      <ConversationDocumentList documents={[doc()]} onRemove={vi.fn()} />,
    );
    expect(
      screen.getByLabelText(en.chat.composer.documents.panelTitle),
    ).toBeDefined();
  });
});

describe("<ConversationDocumentList> index state (D-FV0-31)", () => {
  it("hands each chip its document's index state", () => {
    const { container } = render(
      <NextIntlClientProvider locale="en" messages={en}>
        <ConversationDocumentList
          documents={[
            doc({
              doc_ref: "a",
              index_state: "indexing",
              strategy: "retrieval",
            }),
            doc({ doc_ref: "b" }),
          ]}
          onRemove={() => {}}
        />
      </NextIntlClientProvider>,
    );
    const chips = container.querySelectorAll('[data-slot="document-chip"]');
    expect(chips[0].getAttribute("data-index-state")).toBe("indexing");
    expect(chips[0].textContent).toContain("Indexing…");
    expect(chips[1].textContent).not.toContain("Indexing…");
  });
});
