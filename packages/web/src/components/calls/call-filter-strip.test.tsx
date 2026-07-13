/**
 * R9-028 (c) — calls-page filter strip: built with the R9-014 fix already in
 * place (bounded chip rail; search icon inside the input), plus the same
 * no-infinite-navigation-loop guard `<ConversationFilterStrip>` carries.
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CallFilterStrip } from "./call-filter-strip";

const replace = vi.fn();
let searchString = "";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace }),
  // New instance per call — mirrors Next's real useSearchParams reference churn.
  useSearchParams: () => new URLSearchParams(searchString),
}));

const messages = {
  calls: {
    allPersonas: "All personas",
    searchPlaceholder: "Search calls",
    clearSearch: "Clear search",
  },
};

const manyPersonas = Array.from({ length: 12 }, (_, i) => ({
  id: `p${i}`,
  name: `Persona ${i}`,
  avatar_url: null,
}));

function renderStrip(
  personas: Array<{ id: string; name: string; avatar_url: string | null }> = [],
) {
  return render(
    <NextIntlClientProvider locale="en" messages={messages}>
      <CallFilterStrip personas={personas} />
    </NextIntlClientProvider>,
  );
}

describe("CallFilterStrip — no infinite navigation loop", () => {
  beforeEach(() => {
    replace.mockClear();
    searchString = "";
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("does not navigate when q already matches the URL (no-op → no loop)", () => {
    renderStrip();
    vi.advanceTimersByTime(2000);
    expect(replace).not.toHaveBeenCalled();
  });

  it("pushes q to the URL exactly once after debounce when the user types", () => {
    renderStrip();
    fireEvent.change(screen.getByLabelText("Search calls"), {
      target: { value: "bergen" },
    });
    vi.advanceTimersByTime(300);
    expect(replace).toHaveBeenCalledTimes(1);
    expect(replace).toHaveBeenCalledWith("?q=bergen", { scroll: false });
  });
});

describe("CallFilterStrip — R9-014 parity (bounded rail / icon-inside-input)", () => {
  beforeEach(() => {
    replace.mockClear();
    searchString = "";
  });

  it("puts the chips in a bounded overflow rail so the search stays stable", () => {
    const { container } = renderStrip(manyPersonas);
    const rail = container.querySelector('[data-slot="filter-chip-rail"]');
    expect(rail).not.toBeNull();
    expect(rail).toHaveClass("chip-rail");
    expect(rail?.querySelectorAll("button").length).toBe(
      manyPersonas.length + 1,
    );
    const stableWrap = screen
      .getByLabelText("Search calls")
      .closest("div")?.parentElement;
    expect(stableWrap).toHaveClass("shrink-0");
  });

  it("still sets ?persona_id when a chip is clicked", () => {
    renderStrip(manyPersonas);
    fireEvent.click(screen.getByRole("button", { name: "Persona 3" }));
    expect(replace).toHaveBeenCalledWith("?persona_id=p3", { scroll: false });
  });

  it("renders the search magnifier inside the input adornment slot", () => {
    const { container } = renderStrip(manyPersonas);
    const icon = container.querySelector('[data-slot="input-icon"]');
    expect(icon).not.toBeNull();
    const wrapper = icon?.parentElement;
    expect(wrapper?.querySelector("input")).toBe(
      screen.getByLabelText("Search calls"),
    );
  });
});
