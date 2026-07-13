/**
 * R9-029 — the desktop transcript right panel: turns attributed by AVATAR
 * (persona-avatar / user-avatar, reused per R9-014) instead of "You:" /
 * "{Persona}:" text; a visible AUTO chip surfaces the pinned live-follow
 * state and re-pins (jumps to latest) on click; markdown for finalized
 * persona turns only (mirrors VoiceCaptions' own split).
 */
import { render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it, vi } from "vitest";
import messages from "@/i18n/messages/en.json";
import type { CaptionSegment } from "@/lib/voice/captions";
import { VoiceTranscriptPanel } from "./voice-transcript-panel";

vi.mock("@/auth", () => ({
  useAccount: () => ({
    name: "Tester",
    email: null,
    imageUrl: null,
    available: true,
  }),
}));

const PERSONA = { id: "astrid", name: "Astrid Berg", avatar_url: null };

function renderPanel(captions: CaptionSegment[]) {
  return render(
    <NextIntlClientProvider locale="en" messages={messages}>
      <VoiceTranscriptPanel captions={captions} persona={PERSONA} />
    </NextIntlClientProvider>,
  );
}

describe("VoiceTranscriptPanel", () => {
  it("attributes turns by avatar, not a You/persona-name text label", () => {
    const captions: CaptionSegment[] = [
      { segmentId: "s1", speaker: "user", text: "hello there", isFinal: true },
      {
        segmentId: "s2",
        speaker: "persona",
        text: "hi, how can I help?",
        isFinal: true,
      },
    ];
    renderPanel(captions);
    // The turn text renders …
    expect(screen.getByText("hello there")).toBeInTheDocument();
    // … but visually there is no "You:" / "Astrid Berg:" label node — only an
    // sr-only one for accessibility (avatars carry the visual attribution).
    const rows = document.querySelectorAll(
      '[data-slot="voice-transcript-turn"]',
    );
    expect(rows.length).toBe(2);
    const [userRow, personaRow] = Array.from(rows);
    expect(userRow.getAttribute("data-speaker")).toBe("user");
    expect(personaRow.getAttribute("data-speaker")).toBe("persona");
    // Persona avatar renders (initials-mark, role=img, aria-label=persona name).
    expect(
      personaRow.querySelector('[role="img"][aria-label="Astrid Berg"]'),
    ).not.toBeNull();
  });

  it("renders finalized persona turns as Markdown but leaves user/partial turns plain", () => {
    const captions: CaptionSegment[] = [
      {
        segmentId: "s1",
        speaker: "persona",
        text: "**bold** reply",
        isFinal: true,
      },
      {
        segmentId: "s2",
        speaker: "user",
        text: "**not markdown**",
        isFinal: true,
      },
    ];
    renderPanel(captions);
    // The persona's finalized text renders through the Markdown renderer (bold).
    const strong = document.querySelector("strong");
    expect(strong?.textContent).toBe("bold");
    // The user's turn stays literal — no markdown parsing.
    expect(screen.getByText("**not markdown**")).toBeInTheDocument();
  });

  it("the AUTO chip reflects the pinned state and is a live-follow toggle", () => {
    renderPanel([
      { segmentId: "s1", speaker: "user", text: "hi", isFinal: true },
    ]);
    const chip = screen.getByRole("button", {
      name: "Follow the live transcript",
    });
    // Pinned by default (mirrors VoiceCaptions' initial `pinned = true`).
    expect(chip).toHaveAttribute("aria-pressed", "true");
    expect(chip.getAttribute("data-state")).toBe("active");
  });

  it("carries a screen-reader-only speaker label per turn (accessible even though avatars replace visible text)", () => {
    renderPanel([
      {
        segmentId: "s1",
        speaker: "persona",
        text: "hello",
        isFinal: true,
      },
    ]);
    expect(screen.getByText("Astrid Berg:")).toHaveClass("sr-only");
  });
});
