/**
 * Spec K11 (T5, D-K11-7) — `<EpisodicManagerModal>` component tests.
 *
 * `<EpisodicGraph>` (the real content) is unit-tested on its own
 * (`episodic-graph.test.tsx`); this asserts the modal's OWN contract: it
 * mounts `<EpisodicGraph>` with the personas it's given, opens on trigger
 * click, and closes on the close button — both the uncontrolled `trigger`
 * shape (persona-page glance row) and the controlled `open`/`onOpenChange`
 * shape (chat header's nested-in-a-`<Link>` trigger) that K11-T5 needs.
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it, vi } from "vitest";
import type { AvatarPersona } from "@/components/persona/persona-avatar";
import { EpisodicManagerModal } from "./episodic-manager-modal";

vi.mock("./episodic-graph", () => ({
  EpisodicGraph: ({ personas }: { personas: readonly AvatarPersona[] }) => (
    <div data-testid="episodic-graph">
      {personas.map((p) => p.name).join(", ")}
    </div>
  ),
}));

const messages = {
  memory: {
    episodicManagerTitle: "What they've recalled",
    close: "Close",
  },
};

const PERSONAS: AvatarPersona[] = [{ id: "astrid", name: "Astrid Berg" }];

function renderModal(
  props: Partial<React.ComponentProps<typeof EpisodicManagerModal>> = {},
) {
  return render(
    <NextIntlClientProvider locale="en" messages={messages}>
      <EpisodicManagerModal
        personas={PERSONAS}
        trigger={
          <button type="button" data-testid="trigger">
            open
          </button>
        }
        {...props}
      />
    </NextIntlClientProvider>,
  );
}

describe("EpisodicManagerModal", () => {
  it("is closed until the trigger is clicked, then mounts <EpisodicGraph> with the given personas", () => {
    renderModal();
    expect(screen.queryByTestId("episodic-graph")).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId("trigger"));

    expect(screen.getByText("What they've recalled")).toBeInTheDocument();
    expect(screen.getByTestId("episodic-graph")).toHaveTextContent(
      "Astrid Berg",
    );
  });

  it("closes on the close button", () => {
    renderModal();
    fireEvent.click(screen.getByTestId("trigger"));
    expect(screen.getByTestId("episodic-graph")).toBeInTheDocument();

    fireEvent.click(screen.getByLabelText("Close"));
    expect(screen.queryByTestId("episodic-graph")).not.toBeInTheDocument();
  });

  it("supports the controlled open/onOpenChange shape (no trigger element)", () => {
    const onOpenChange = vi.fn();
    const { rerender } = render(
      <NextIntlClientProvider locale="en" messages={messages}>
        <EpisodicManagerModal
          personas={PERSONAS}
          open={false}
          onOpenChange={onOpenChange}
        />
      </NextIntlClientProvider>,
    );
    expect(screen.queryByTestId("episodic-graph")).not.toBeInTheDocument();

    rerender(
      <NextIntlClientProvider locale="en" messages={messages}>
        <EpisodicManagerModal
          personas={PERSONAS}
          open={true}
          onOpenChange={onOpenChange}
        />
      </NextIntlClientProvider>,
    );
    expect(screen.getByTestId("episodic-graph")).toBeInTheDocument();
  });
});
