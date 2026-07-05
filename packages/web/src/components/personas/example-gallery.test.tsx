/**
 * ExampleGallery — renders the curated starter set and hands a picked example
 * back to its parent (the seed → describe-textarea handoff lives in the wizard).
 *
 * Verifies: every category heading + card renders; clicking a card fires
 * onSelect with the full example object; the selected card exposes the
 * "added" affordance; each card carries an accessible label naming the persona.
 */
import { fireEvent, render } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it, vi } from "vitest";
import messages from "@/i18n/messages/en.json";
import { PERSONA_EXAMPLE_CATEGORIES } from "@/lib/persona-examples";
import { CATEGORY_LABEL_KEY, ExampleGallery } from "./example-gallery";

function renderWith(ui: React.ReactNode) {
  return render(
    <NextIntlClientProvider locale="en" messages={messages}>
      {ui}
    </NextIntlClientProvider>,
  );
}

it("every starter category maps to a non-fallback label (R4-C1-1)", () => {
  // The Spec-36 roster grew categories faster than the label map/en.json; an
  // unmapped id renders the bare "author" namespace fallback + a MISSING_MESSAGE
  // console error. This pins the contract: every category id resolves a real label.
  const gallery = (messages as { author: { gallery: Record<string, string> } })
    .author.gallery;
  for (const category of PERSONA_EXAMPLE_CATEGORIES) {
    const key = CATEGORY_LABEL_KEY[category.id];
    expect(
      key,
      `category "${category.id}" has no label-key mapping`,
    ).toBeTruthy();
    const suffix = key.split(".")[1]; // "gallery.categoryX" -> "categoryX"
    expect(
      gallery[suffix],
      `label "${key}" missing from en.json (would render the "author" fallback)`,
    ).toBeTruthy();
  }
});

const ALL_EXAMPLES = PERSONA_EXAMPLE_CATEGORIES.flatMap((c) => c.examples);

describe("ExampleGallery", () => {
  it("renders every category heading and every example card", () => {
    const { container, getByText } = renderWith(
      <ExampleGallery onSelect={() => {}} />,
    );

    // Six category sections.
    expect(
      container.querySelectorAll('[data-slot="example-category"]'),
    ).toHaveLength(PERSONA_EXAMPLE_CATEGORIES.length);

    // Every example card is present.
    expect(
      container.querySelectorAll('[data-slot="example-card"]'),
    ).toHaveLength(ALL_EXAMPLES.length);

    // Category labels resolve through i18n (spot-check two).
    expect(getByText(messages.author.gallery.categoryWork)).toBeInTheDocument();
    expect(
      getByText(messages.author.gallery.categoryCompanionship),
    ).toBeInTheDocument();
  });

  it("fires onSelect with the full example when a card is clicked", () => {
    const onSelect = vi.fn();
    const target = ALL_EXAMPLES[0];
    const { getByLabelText } = renderWith(
      <ExampleGallery onSelect={onSelect} />,
    );

    const label = messages.author.gallery.useNamed.replace(
      "{name}",
      target.name,
    );
    fireEvent.click(getByLabelText(label));

    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onSelect).toHaveBeenCalledWith(target);
  });

  it("marks the selected card with the added affordance", () => {
    const target = ALL_EXAMPLES[2];
    const { getByLabelText, getByText } = renderWith(
      <ExampleGallery onSelect={() => {}} selectedId={target.id} />,
    );

    const label = messages.author.gallery.useNamed.replace(
      "{name}",
      target.name,
    );
    expect(getByLabelText(label).getAttribute("data-selected")).toBe("true");
    expect(getByText(messages.author.gallery.selected)).toBeInTheDocument();
  });

  it("gives every card an accessible label naming its persona", () => {
    const { getByLabelText } = renderWith(
      <ExampleGallery onSelect={() => {}} />,
    );
    for (const example of ALL_EXAMPLES) {
      const label = messages.author.gallery.useNamed.replace(
        "{name}",
        example.name,
      );
      expect(getByLabelText(label)).toBeInTheDocument();
    }
  });
});
