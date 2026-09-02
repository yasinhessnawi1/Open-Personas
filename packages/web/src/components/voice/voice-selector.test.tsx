import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it, vi } from "vitest";
import { VoiceSelector } from "./voice-selector";

vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ getToken: async () => "jwt" }),
}));

// Keep the real `voiceDisplayName` (the name-stripping helper the selector uses)
// and override only the network fetch.
vi.mock("@/lib/voice/voices", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/voice/voices")>();
  return {
    ...actual,
    fetchVoices: vi.fn(async () => ({
      provider: "cartesia",
      voices: [
        {
          voice_id: "v1",
          name: "Clara - Warm Storyteller",
          gender: "feminine",
          language: "en",
          description: "warm",
          preview_url: "https://cdn/c.mp3",
        },
      ],
    })),
  };
});

const messages = {
  voice: {
    voiceDefault: "Default voice",
    voicePreview: "Preview",
    voiceStop: "Stop",
    voiceSelected: "Selected",
    voicesLoading: "Loading voices…",
    voicesUnavailable: "Voice selection is unavailable right now.",
    voicesError: "Couldn't load voices.",
    voiceProviderMismatch:
      "This persona's saved voice isn't available from the current voice provider. Choose one below to update it.",
  },
};

function renderSelector(props: {
  value?: string | null;
  onChange: (v: { provider: string; voice_id: string } | null) => void;
}) {
  return render(
    <NextIntlClientProvider locale="en" messages={messages}>
      <VoiceSelector {...props} />
    </NextIntlClientProvider>,
  );
}

describe("VoiceSelector (C2)", () => {
  it("lists catalogue voices; choosing one sets the full {provider, voice_id}", async () => {
    const onChange = vi.fn();
    renderSelector({ value: null, onChange });
    // The picker strips the provider's human name ("Clara") to the descriptor.
    await waitFor(() =>
      expect(screen.getByText("Warm Storyteller")).toBeInTheDocument(),
    );

    fireEvent.click(screen.getByText("Warm Storyteller"));
    expect(onChange).toHaveBeenCalledWith({
      provider: "cartesia",
      voice_id: "v1",
    });
  });

  it("offers a default option that clears the persona's voice", async () => {
    const onChange = vi.fn();
    renderSelector({ value: "v1", onChange });
    await waitFor(() =>
      expect(screen.getByText("Default voice")).toBeInTheDocument(),
    );

    fireEvent.click(screen.getByText("Default voice"));
    expect(onChange).toHaveBeenCalledWith(null);
  });

  it("renders a preview control for a voice that has a sample", async () => {
    renderSelector({ value: null, onChange: vi.fn() });
    await waitFor(() =>
      expect(screen.getByLabelText("Preview")).toBeInTheDocument(),
    );
  });

  it("nudges to re-pick when the saved voice is from another provider (V14 D-V14-13)", async () => {
    // The catalogue only has "v1"; a saved id not in it was picked under a
    // different provider (a provider switch) → the mismatch nudge shows.
    renderSelector({ value: "cartesia-legacy-voice", onChange: vi.fn() });
    await waitFor(() =>
      expect(screen.getByText("Warm Storyteller")).toBeInTheDocument(),
    );
    expect(
      screen.getByText(
        "This persona's saved voice isn't available from the current voice provider. Choose one below to update it.",
      ),
    ).toBeInTheDocument();
  });

  it("shows NO mismatch nudge when the saved voice is in the catalogue", async () => {
    renderSelector({ value: "v1", onChange: vi.fn() });
    await waitFor(() =>
      expect(screen.getByText("Warm Storyteller")).toBeInTheDocument(),
    );
    expect(
      screen.queryByText(/isn't available from the current voice provider/),
    ).not.toBeInTheDocument();
  });
});
