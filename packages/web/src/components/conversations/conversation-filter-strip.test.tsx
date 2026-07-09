/**
 * Regression test for the conversations infinite-navigation loop.
 *
 * Bug: the debounced search effect called router.replace() unconditionally,
 * with `search` (useSearchParams — a fresh reference every render) in its dep
 * array. Each replace → RSC refetch → new `search` ref → effect re-fires →
 * replace … forever (GET /conversations indefinitely, even with no input).
 *
 * The mock returns a NEW URLSearchParams on every call, faithfully reproducing
 * the unstable-reference condition that drove the loop.
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ConversationFilterStrip } from "./conversation-filter-strip";

const replace = vi.fn();
let searchString = "";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace }),
  // New instance per call — mirrors Next's real useSearchParams reference churn.
  useSearchParams: () => new URLSearchParams(searchString),
}));

const messages = {
  conversations: {
    allPersonas: "All",
    searchPlaceholder: "Search",
    delete: "Clear",
  },
};

function renderStrip() {
  return render(
    <NextIntlClientProvider locale="en" messages={messages}>
      <ConversationFilterStrip personas={[]} />
    </NextIntlClientProvider>,
  );
}

describe("ConversationFilterStrip — no infinite navigation loop", () => {
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
    // Let any debounce timers fire repeatedly; the guard must keep replace silent.
    vi.advanceTimersByTime(2000);
    expect(replace).not.toHaveBeenCalled();
  });

  it("pushes q to the URL exactly once after debounce when the user types", () => {
    renderStrip();
    fireEvent.change(screen.getByLabelText("Search"), {
      target: { value: "hello" },
    });
    vi.advanceTimersByTime(300);
    expect(replace).toHaveBeenCalledTimes(1);
    expect(replace).toHaveBeenCalledWith("?q=hello", { scroll: false });
  });
});

const manyPersonas = Array.from({ length: 12 }, (_, i) => ({
  id: `p${i}`,
  name: `Persona ${i}`,
  avatar_url: null,
}));

function renderStripWith(
  personas: Array<{ id: string; name: string; avatar_url: string | null }>,
) {
  return render(
    <NextIntlClientProvider locale="en" messages={messages}>
      <ConversationFilterStrip personas={personas} />
    </NextIntlClientProvider>,
  );
}

describe("ConversationFilterStrip — R9-014 (a) chip rail / stable search", () => {
  beforeEach(() => {
    replace.mockClear();
    searchString = "";
  });

  it("puts the chips in a bounded overflow rail so the search stays stable", () => {
    const { container } = renderStripWith(manyPersonas);
    // (a): chips live inside the bounded scroll rail (overflow-x), NOT flex-wrap.
    const rail = container.querySelector('[data-slot="filter-chip-rail"]');
    expect(rail).not.toBeNull();
    expect(rail).toHaveClass("chip-rail");
    // Every persona (+ "All") is still rendered — the rail scrolls, none dropped.
    expect(rail?.querySelectorAll("button").length).toBe(
      manyPersonas.length + 1,
    );
    // The search field keeps a fixed-width container (shrink-0) beside the rail
    // — the input sits in its icon-adornment wrapper, whose parent is stable.
    const stableWrap = screen
      .getByLabelText("Search")
      .closest("div")?.parentElement;
    expect(stableWrap).toHaveClass("shrink-0");
  });

  it("still sets ?persona_id when a chip is clicked", () => {
    renderStripWith(manyPersonas);
    fireEvent.click(screen.getByRole("button", { name: "Persona 3" }));
    expect(replace).toHaveBeenCalledWith("?persona_id=p3", { scroll: false });
  });

  it("renders the search magnifier inside the input adornment slot", () => {
    const { container } = renderStripWith(manyPersonas);
    const icon = container.querySelector('[data-slot="input-icon"]');
    expect(icon).not.toBeNull();
    // The adornment sits in the same relative wrapper as the input (icon-left).
    const wrapper = icon?.parentElement;
    // The search input is the icon's sibling inside the same adornment wrapper.
    expect(wrapper?.querySelector("input")).toBe(
      screen.getByLabelText("Search"),
    );
  });
});
