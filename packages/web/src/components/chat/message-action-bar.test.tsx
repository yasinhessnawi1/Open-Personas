/**
 * R9-025a — MessageActionBar tests: copy (both roles), retry (persona role,
 * active-turn disabled), read aloud (persona role, loading/playing states +
 * fail-soft hiding on the proxy's 503 `voice_unavailable`).
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import type { ComponentProps } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ getToken: () => Promise.resolve("test-token") }),
}));
vi.mock("@/auth", () => ({
  useAuth: () => ({ getToken: () => Promise.resolve("test-token") }),
}));

const fetchPersonaTtsMock = vi.fn();
vi.mock("@/lib/voice/tts", () => ({
  fetchPersonaTts: (...args: unknown[]) => fetchPersonaTtsMock(...args),
}));

import { ApiError } from "@/lib/api/client";
import { MessageActionBar } from "./message-action-bar";

const messages = {
  chat: {
    actions: {
      copy: "Copy message",
      copied: "Copied",
      retry: "Retry",
      edit: "Edit message",
      readAloud: "Read aloud",
      stopReading: "Stop reading",
      loadingAudio: "Loading audio…",
      turnIntoFile: {
        label: "Turn into file",
        formatMenu: "Choose file format",
        toast: "Creating file…",
        errorToast: "Couldn't start the file — try again",
        format: {
          pdf: "PDF",
          md: "Markdown",
          xlsx: "Excel",
          csv: "CSV",
        },
      },
    },
  },
};

function renderBar(
  props: Partial<ComponentProps<typeof MessageActionBar>> = {},
) {
  return render(
    <NextIntlClientProvider locale="en" messages={messages}>
      <MessageActionBar
        messageRole="persona"
        content="Hello there"
        {...props}
      />
    </NextIntlClientProvider>,
  );
}

describe("MessageActionBar", () => {
  beforeEach(() => {
    fetchPersonaTtsMock.mockReset();
    Object.assign(navigator, {
      clipboard: { writeText: vi.fn().mockResolvedValue(undefined) },
    });
    globalThis.URL.createObjectURL = vi.fn(() => "blob:test-1");
    globalThis.URL.revokeObjectURL = vi.fn();
    // jsdom doesn't implement HTMLMediaElement playback.
    window.HTMLMediaElement.prototype.play = vi
      .fn()
      .mockResolvedValue(undefined);
    window.HTMLMediaElement.prototype.pause = vi.fn();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  describe("copy — both roles", () => {
    it("writes the message content to the clipboard and shows a transient copied state", async () => {
      renderBar({ messageRole: "user", personaId: undefined });
      fireEvent.click(screen.getByRole("button", { name: "Copy message" }));
      await waitFor(() =>
        expect(navigator.clipboard.writeText).toHaveBeenCalledWith(
          "Hello there",
        ),
      );
      await waitFor(() =>
        expect(screen.getByRole("button", { name: "Copied" })).toBeTruthy(),
      );
    });

    it("renders the copy button for a persona message too", () => {
      renderBar({ messageRole: "persona" });
      expect(screen.getByRole("button", { name: "Copy message" })).toBeTruthy();
    });
  });

  describe("role gating", () => {
    it("user role: only the copy button renders (no retry, no edit, no read-aloud) when onEdit isn't wired", () => {
      renderBar({ messageRole: "user", onRetry: () => {}, personaId: "p1" });
      expect(screen.getByRole("button", { name: "Copy message" })).toBeTruthy();
      expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
      expect(screen.queryByRole("button", { name: "Edit message" })).toBeNull();
      expect(screen.queryByRole("button", { name: "Read aloud" })).toBeNull();
    });

    it("persona role: no edit affordance even when onEdit is (nonsensically) wired", () => {
      renderBar({ messageRole: "persona", onEdit: () => {}, personaId: "p1" });
      expect(screen.queryByRole("button", { name: "Edit message" })).toBeNull();
    });

    it("persona role without onRetry wired: no dead retry affordance", () => {
      renderBar({
        messageRole: "persona",
        personaId: "p1",
        onRetry: undefined,
      });
      expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
    });

    it("persona role without personaId: no read-aloud affordance", () => {
      renderBar({ messageRole: "persona", personaId: undefined });
      expect(screen.queryByRole("button", { name: "Read aloud" })).toBeNull();
    });
  });

  describe("retry", () => {
    it("calls onRetry when clicked", () => {
      const onRetry = vi.fn();
      renderBar({ messageRole: "persona", onRetry, personaId: undefined });
      fireEvent.click(screen.getByRole("button", { name: "Retry" }));
      expect(onRetry).toHaveBeenCalledTimes(1);
    });

    it("is disabled while a turn is active (retryDisabled) and does not fire", () => {
      const onRetry = vi.fn();
      renderBar({
        messageRole: "persona",
        onRetry,
        retryDisabled: true,
        personaId: undefined,
      });
      const button = screen.getByRole("button", {
        name: "Retry",
      }) as HTMLButtonElement;
      expect(button.disabled).toBe(true);
      fireEvent.click(button);
      expect(onRetry).not.toHaveBeenCalled();
    });
  });

  describe("edit (R9-025 leg C)", () => {
    it("calls onEdit when clicked", () => {
      const onEdit = vi.fn();
      renderBar({ messageRole: "user", onEdit, personaId: undefined });
      fireEvent.click(screen.getByRole("button", { name: "Edit message" }));
      expect(onEdit).toHaveBeenCalledTimes(1);
    });

    it("is disabled while a turn is active (editDisabled) and does not fire", () => {
      const onEdit = vi.fn();
      renderBar({
        messageRole: "user",
        onEdit,
        editDisabled: true,
        personaId: undefined,
      });
      const button = screen.getByRole("button", {
        name: "Edit message",
      }) as HTMLButtonElement;
      expect(button.disabled).toBe(true);
      fireEvent.click(button);
      expect(onEdit).not.toHaveBeenCalled();
    });
  });

  describe("read aloud", () => {
    it("fetches + plays audio: idle -> loading -> playing", async () => {
      let resolveTts: (blob: Blob) => void = () => {};
      fetchPersonaTtsMock.mockReturnValue(
        new Promise((resolve) => {
          resolveTts = resolve;
        }),
      );
      renderBar({ messageRole: "persona", personaId: "persona_astrid" });
      fireEvent.click(screen.getByRole("button", { name: "Read aloud" }));

      await waitFor(() =>
        expect(
          screen.getByRole("button", { name: "Loading audio…" }),
        ).toBeTruthy(),
      );
      resolveTts(new Blob([new Uint8Array([1, 2, 3])], { type: "audio/wav" }));

      await waitFor(() =>
        expect(
          screen.getByRole("button", { name: "Stop reading" }),
        ).toBeTruthy(),
      );
      expect(fetchPersonaTtsMock).toHaveBeenCalledWith(
        "persona_astrid",
        "Hello there",
        expect.anything(),
      );
    });

    it("hides the button entirely on a 503 (fail-soft feature-absent)", async () => {
      fetchPersonaTtsMock.mockRejectedValue(
        new ApiError(
          503,
          { error: "voice_unavailable" },
          { limit: null, remaining: null, reset: null, retryAfter: null },
        ),
      );
      renderBar({ messageRole: "persona", personaId: "persona_astrid" });
      fireEvent.click(screen.getByRole("button", { name: "Read aloud" }));

      await waitFor(() =>
        expect(screen.queryByRole("button", { name: "Read aloud" })).toBeNull(),
      );
      expect(
        screen.queryByRole("button", { name: "Loading audio…" }),
      ).toBeNull();
      expect(screen.queryByRole("button", { name: "Stop reading" })).toBeNull();
    });

    it("returns to idle (stays visible) on a non-503 error", async () => {
      fetchPersonaTtsMock.mockRejectedValue(new Error("network blip"));
      renderBar({ messageRole: "persona", personaId: "persona_astrid" });
      fireEvent.click(screen.getByRole("button", { name: "Read aloud" }));

      await waitFor(() =>
        expect(screen.getByRole("button", { name: "Read aloud" })).toBeTruthy(),
      );
    });

    it("clicking again while playing stops playback (toggle)", async () => {
      fetchPersonaTtsMock.mockResolvedValue(
        new Blob([new Uint8Array([1])], { type: "audio/wav" }),
      );
      renderBar({ messageRole: "persona", personaId: "persona_astrid" });
      fireEvent.click(screen.getByRole("button", { name: "Read aloud" }));
      await waitFor(() =>
        expect(
          screen.getByRole("button", { name: "Stop reading" }),
        ).toBeTruthy(),
      );
      fireEvent.click(screen.getByRole("button", { name: "Stop reading" }));
      await waitFor(() =>
        expect(screen.getByRole("button", { name: "Read aloud" })).toBeTruthy(),
      );
      // A second click after stopping fetches again (no client-side cache).
      expect(fetchPersonaTtsMock).toHaveBeenCalledTimes(1);
    });
  });

  describe("turn into file — R9-025b", () => {
    it("renders for a persona message when onTurnIntoFile is wired", () => {
      renderBar({
        messageRole: "persona",
        personaId: undefined,
        onTurnIntoFile: () => {},
      });
      expect(
        screen.getByRole("button", { name: "Turn into file" }),
      ).toBeTruthy();
      expect(
        screen.getByRole("button", { name: "Choose file format" }),
      ).toBeTruthy();
    });

    it("does not render for a user message (assistant messages only)", () => {
      renderBar({
        messageRole: "user",
        personaId: undefined,
        onTurnIntoFile: () => {},
      });
      expect(
        screen.queryByRole("button", { name: "Turn into file" }),
      ).toBeNull();
    });

    it("does not render when onTurnIntoFile is not wired (no seam)", () => {
      renderBar({ messageRole: "persona", personaId: undefined });
      expect(
        screen.queryByRole("button", { name: "Turn into file" }),
      ).toBeNull();
    });

    it("the main button fires the calm default (auto) on a plain click", () => {
      const onTurnIntoFile = vi.fn();
      renderBar({
        messageRole: "persona",
        personaId: undefined,
        onTurnIntoFile,
      });
      fireEvent.click(screen.getByRole("button", { name: "Turn into file" }));
      expect(onTurnIntoFile).toHaveBeenCalledWith("auto");
    });

    it.each([
      ["PDF", "pdf"],
      ["Markdown", "md"],
      ["Excel", "xlsx"],
      ["CSV", "csv"],
    ])("the format menu fires the explicit %s pick", async (label, format) => {
      const onTurnIntoFile = vi.fn();
      renderBar({
        messageRole: "persona",
        personaId: undefined,
        onTurnIntoFile,
      });
      fireEvent.click(
        screen.getByRole("button", { name: "Choose file format" }),
      );
      const item = await screen.findByRole("menuitem", { name: label });
      fireEvent.click(item);
      expect(onTurnIntoFile).toHaveBeenCalledWith(format);
    });

    it("is disabled while a turn is active (turnIntoFileDisabled) and does not fire", () => {
      const onTurnIntoFile = vi.fn();
      renderBar({
        messageRole: "persona",
        personaId: undefined,
        onTurnIntoFile,
        turnIntoFileDisabled: true,
      });
      const button = screen.getByRole("button", {
        name: "Turn into file",
      }) as HTMLButtonElement;
      expect(button.disabled).toBe(true);
      fireEvent.click(button);
      expect(onTurnIntoFile).not.toHaveBeenCalled();
    });
  });
});
