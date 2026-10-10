import { fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it, vi } from "vitest";
import en from "@/i18n/messages/en.json";
import { DocumentChip } from "./document-chip";

function renderWithIntl(ui: React.ReactNode) {
  return render(
    <NextIntlClientProvider locale="en" messages={en}>
      {ui}
    </NextIntlClientProvider>,
  );
}

describe("<DocumentChip>", () => {
  it("renders filename + format + size", () => {
    renderWithIntl(
      <DocumentChip
        docRef="r-1"
        filename="contract.pdf"
        format="pdf"
        sizeBytes={50_000}
      />,
    );
    expect(screen.getByText("contract.pdf")).toBeDefined();
    expect(screen.getByText(/PDF/)).toBeDefined();
    expect(screen.getByText(/49 KB/)).toBeDefined();
  });

  it("hides size when sizeBytes is null", () => {
    renderWithIntl(
      <DocumentChip
        docRef="r-2"
        filename="notes.md"
        format="md"
        sizeBytes={null}
      />,
    );
    const caption = screen.getByText(/MD/);
    expect(caption.textContent).toBe("MD");
  });

  it("scanned-PDF cue (T18) renders when strategy=vision_handoff", () => {
    renderWithIntl(
      <DocumentChip
        docRef="r-3"
        filename="scanned.pdf"
        format="pdf"
        sizeBytes={1_000_000}
        strategy="vision_handoff"
      />,
    );
    // The cue is rendered as a span with the scannedCue i18n string as aria-label.
    expect(
      screen.getByLabelText(en.chat.composer.documents.scannedCue),
    ).toBeDefined();
  });

  it("hides scanned cue for whole_inject / retrieval strategies", () => {
    renderWithIntl(
      <DocumentChip
        docRef="r-4"
        filename="report.docx"
        format="docx"
        sizeBytes={20_000}
        strategy="whole_inject"
      />,
    );
    expect(
      screen.queryByLabelText(en.chat.composer.documents.scannedCue),
    ).toBeNull();
  });

  it("calls onRemove with docRef when remove clicked", () => {
    const onRemove = vi.fn();
    renderWithIntl(
      <DocumentChip
        docRef="r-5"
        filename="r.pdf"
        format="pdf"
        sizeBytes={100}
        onRemove={onRemove}
      />,
    );
    fireEvent.click(screen.getByLabelText(en.chat.composer.attach.remove));
    expect(onRemove).toHaveBeenCalledWith("r-5");
  });

  it("hides remove button when onRemove is undefined (read-only chip)", () => {
    renderWithIntl(
      <DocumentChip
        docRef="r-6"
        filename="r.pdf"
        format="pdf"
        sizeBytes={100}
      />,
    );
    expect(screen.queryByLabelText(en.chat.composer.attach.remove)).toBeNull();
  });

  it("ARIA strings come from i18n keys, NOT raw English (T20 discipline)", () => {
    renderWithIntl(
      <DocumentChip
        docRef="r-7"
        filename="r.pdf"
        format="pdf"
        sizeBytes={100}
        strategy="vision_handoff"
        onRemove={vi.fn()}
      />,
    );
    const remove = screen.getByLabelText(en.chat.composer.attach.remove);
    expect(remove.getAttribute("aria-label")).toBe(
      en.chat.composer.attach.remove,
    );
    const scanned = screen.getByLabelText(
      en.chat.composer.documents.scannedCue,
    );
    expect(scanned.getAttribute("aria-label")).toBe(
      en.chat.composer.documents.scannedCue,
    );
  });

  it("data-slot=document-chip enables aggregate selectors in the panel + e2e tests", () => {
    const { container } = renderWithIntl(
      <DocumentChip
        docRef="r-8"
        filename="r.pdf"
        format="pdf"
        sizeBytes={100}
      />,
    );
    const chip = container.querySelector("[data-slot='document-chip']");
    expect(chip).not.toBeNull();
    expect(chip?.getAttribute("data-format")).toBe("pdf");
  });
});

describe("<DocumentChip> index state (D-FV0-31)", () => {
  const cases = [
    ["pending", false, "Indexing…"],
    ["indexing", false, "Indexing…"],
    ["partial", false, "Partly indexed"],
    ["failed", false, "Couldn't index"],
    ["not_needed", true, "Partly read"],
    ["failed", true, "Couldn't index"],
  ] as const;

  for (const [indexState, textPartial, label] of cases) {
    it(`shows "${label}" for ${indexState}${textPartial ? " and a cut text" : ""}`, () => {
      const { container } = renderWithIntl(
        <DocumentChip
          docRef="r-9"
          filename="report.pdf"
          format="pdf"
          sizeBytes={2048}
          indexState={indexState}
          textPartial={textPartial}
        />,
      );
      const state = container.querySelectorAll('[data-slot="document-state"]');
      expect(state).toHaveLength(1);
      expect(state[0].textContent).toBe(` · ${label}`);
    });
  }

  for (const indexState of ["ready", "not_needed"] as const) {
    it(`shows no label for a ${indexState} document read whole`, () => {
      const { container } = renderWithIntl(
        <DocumentChip
          docRef="r-10"
          filename="memo.txt"
          format="txt"
          sizeBytes={2048}
          indexState={indexState}
          textPartial={false}
        />,
      );
      expect(
        container.querySelectorAll('[data-slot="document-state"]'),
      ).toHaveLength(0);
    });
  }
});
