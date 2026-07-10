/**
 * Spec M1 (T7) — <PersonaModelPicker> reusable component tests.
 *
 * Mirrors persona-picker.test.tsx's composition + test style (R9-014): the
 * picker renders whatever `models` it's given, filters locally between the
 * curated shortlist and "browse all", and reports the chosen id (or `null`
 * for "use the tier default") — it never fetches, never knows what selection
 * does.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it, vi } from "vitest";
import type { ModelOption } from "@/lib/api/models-client";
import { PersonaModelPicker } from "./persona-model-picker";

const messages = {
  modelPicker: {
    title: "Model",
    tierDefault: "Use tier default — routes by task",
    browseAll: "Browse all models",
    pricePerM: "{in} in / {out} out per 1M tokens",
    empty: "No models available right now — using the tier default.",
    recommended: "Recommended",
  },
};

const RECOMMENDED_A: ModelOption = {
  id: "anthropic/claude-sonnet-4.6",
  label: "Claude Sonnet 4.6",
  provider: "anthropic",
  input_price_per_1m: 3,
  output_price_per_1m: 15,
  context_length: 200000,
  tools_supported: true,
  recommended: true,
};
const RECOMMENDED_B: ModelOption = {
  id: "z-ai/glm-4.6",
  label: "GLM 4.6",
  provider: "z-ai",
  input_price_per_1m: 0.5,
  output_price_per_1m: 1.5,
  context_length: 128000,
  tools_supported: true,
  recommended: true,
};
const NICHE: ModelOption = {
  id: "mistral/mixtral-8x22b",
  label: "Mixtral 8x22B",
  provider: "mistral",
  input_price_per_1m: 0.65,
  output_price_per_1m: 0.65,
  context_length: 64000,
  tools_supported: false,
  recommended: false,
};

const MODELS: ModelOption[] = [RECOMMENDED_A, RECOMMENDED_B, NICHE];

function renderPicker(
  models: readonly ModelOption[],
  value: string | null = null,
  onSelect = vi.fn(),
) {
  const utils = render(
    <NextIntlClientProvider locale="en" messages={messages}>
      <PersonaModelPicker models={models} value={value} onSelect={onSelect} />
    </NextIntlClientProvider>,
  );
  return { ...utils, onSelect };
}

async function openMenu() {
  const trigger = screen.getByLabelText(/^Model:/);
  fireEvent.click(trigger);
  // base-ui portals the popup on open — wait for the pinned item to mount.
  await waitFor(() => {
    expect(
      screen.getByRole("menuitem", { name: /Use tier default/ }),
    ).toBeInTheDocument();
  });
}

describe("PersonaModelPicker", () => {
  it("defaults to the recommended set, with price tags, hiding non-recommended models", async () => {
    renderPicker(MODELS);
    await openMenu();
    expect(
      screen.getByRole("menuitem", { name: /Claude Sonnet 4\.6/ }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("$3.00 in / $15.00 out per 1M tokens"),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Mixtral 8x22B/)).not.toBeInTheDocument();
  });

  it("badges recommended models", async () => {
    renderPicker(MODELS);
    await openMenu();
    expect(screen.getAllByText("Recommended").length).toBe(2);
  });

  it("fires onSelect(null) when 'Use tier default' is chosen", async () => {
    const { onSelect } = renderPicker(MODELS, "anthropic/claude-sonnet-4.6");
    await openMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: /Use tier default/ }));
    expect(onSelect).toHaveBeenCalledWith(null);
  });

  it("fires onSelect with the chosen model id", async () => {
    const { onSelect } = renderPicker(MODELS);
    await openMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: /GLM 4\.6/ }));
    expect(onSelect).toHaveBeenCalledWith("z-ai/glm-4.6");
  });

  it("'Browse all' reveals non-recommended models without closing the menu", async () => {
    renderPicker(MODELS);
    await openMenu();
    expect(screen.queryByText(/Mixtral 8x22B/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("menuitem", { name: /Browse all/ }));
    await waitFor(() => {
      expect(screen.getByText(/Mixtral 8x22B/)).toBeInTheDocument();
    });
    // Still open — the recommended item is still reachable in the same menu.
    expect(
      screen.getByRole("menuitem", { name: /Claude Sonnet 4\.6/ }),
    ).toBeInTheDocument();
  });

  it("always shows the current pick even if it isn't in the recommended set", async () => {
    renderPicker(MODELS, "mistral/mixtral-8x22b");
    await openMenu();
    expect(
      screen.getByRole("menuitem", { name: /Mixtral 8x22B/ }),
    ).toBeInTheDocument();
  });

  it("shows the raw id on the trigger when the pinned model is absent from the catalog", () => {
    renderPicker(MODELS, "openai/gpt-5.1-delisted");
    // Exact match: proves the trigger reads the raw id, not the "Use tier
    // default" copy that `t("tierDefault")` would otherwise fall back to.
    expect(
      screen.getByLabelText("Model: openai/gpt-5.1-delisted"),
    ).toBeInTheDocument();
  });

  it("degrades to 'Use tier default' only + the fail-open empty copy when models is empty", async () => {
    renderPicker([]);
    await openMenu();
    expect(
      screen.getByText(
        "No models available right now — using the tier default.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("menuitem", { name: /Browse all/ }),
    ).not.toBeInTheDocument();
  });
});
