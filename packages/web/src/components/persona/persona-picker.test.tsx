/**
 * R9-014 (b) — <PersonaPicker> reusable component tests.
 *
 * Asserts the reusable contract:
 *   - one row per persona = avatar (aria-label = name) + name;
 *   - onSelect fires with the chosen persona id (click + keyboard);
 *   - empty state when there are no personas;
 *   - presentation-agnostic: the picker doesn't know what selection does.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it, vi } from "vitest";
import type { AvatarPersona } from "./persona-avatar";
import { PersonaPicker } from "./persona-picker";

const messages = {
  personaPicker: {
    choosePersona: "Choose a persona",
    empty: "No personas yet. Create one to start a chat.",
  },
};

const PERSONAS: AvatarPersona[] = [
  { id: "astrid", name: "Astrid Berg", avatar_url: null },
  { id: "lena", name: "Lena Brevik", avatar_url: null },
];

function renderPicker(personas: readonly AvatarPersona[], onSelect = vi.fn()) {
  const utils = render(
    <NextIntlClientProvider locale="en" messages={messages}>
      <PersonaPicker personas={personas} onSelect={onSelect} />
    </NextIntlClientProvider>,
  );
  return { ...utils, onSelect };
}

async function openMenu() {
  const trigger = screen.getByLabelText("Choose a persona");
  fireEvent.click(trigger);
  // base-ui portals the popup on open — wait for the first item to mount.
  await waitFor(() => {
    expect(
      screen.getByRole("menuitem", { name: /Astrid Berg/ }),
    ).toBeInTheDocument();
  });
}

describe("PersonaPicker", () => {
  it("renders an avatar + name row per persona when opened", async () => {
    renderPicker(PERSONAS);
    await openMenu();
    // Avatar exposes the persona name as its aria-label (role=img);
    // the name text renders beside it.
    expect(screen.getAllByLabelText("Astrid Berg").length).toBeGreaterThan(0);
    expect(screen.getByText("Lena Brevik")).toBeInTheDocument();
  });

  it("fires onSelect with the persona id on click", async () => {
    const { onSelect } = renderPicker(PERSONAS);
    await openMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: /Lena Brevik/ }));
    expect(onSelect).toHaveBeenCalledWith("lena");
  });

  it("fires onSelect via keyboard (Enter on a focused item)", async () => {
    const { onSelect } = renderPicker(PERSONAS);
    await openMenu();
    const item = screen.getByRole("menuitem", { name: /Astrid Berg/ });
    item.focus();
    fireEvent.keyDown(item, { key: "Enter", code: "Enter" });
    expect(onSelect).toHaveBeenCalledWith("astrid");
  });

  it("shows the empty state when there are no personas", async () => {
    renderPicker([]);
    fireEvent.click(screen.getByLabelText("Choose a persona"));
    await waitFor(() => {
      expect(
        screen.getByText("No personas yet. Create one to start a chat."),
      ).toBeInTheDocument();
    });
  });
});
