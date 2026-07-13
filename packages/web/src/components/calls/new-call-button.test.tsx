/**
 * R9-028 (a) — "New call" flow on the calls page.
 *
 * Mirrors `persona-library-card.test.tsx`'s call-entry coverage: choosing a
 * persona from the reusable picker mints an `origin='call'` conversation, then
 * hands it to the hoisted call session via `requestCall` — navigating only
 * when the session didn't defer to the end-and-switch confirm.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { NewCallButton } from "./new-call-button";

const h = vi.hoisted(() => ({
  push: vi.fn(),
  requestCall: vi.fn(() => "started" as "started" | "current" | "switch"),
  post: vi.fn(async () => ({ data: { id: "new-conv" } })),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: h.push }),
}));
vi.mock("@/lib/api/use-api", () => ({
  useApi: () => ({ POST: h.post, GET: vi.fn(), DELETE: vi.fn() }),
}));
vi.mock("@/lib/voice/call-session-context", () => ({
  useCallSession: () => ({ requestCall: h.requestCall }),
}));

const messages = {
  calls: { newCall: "New call" },
  personaPicker: { choosePersona: "Choose a persona", empty: "None" },
};

const PERSONAS = [
  { id: "astrid", name: "Astrid Berg", avatar_url: null, role: "Tenancy law" },
  { id: "lena", name: "Lena Brevik", avatar_url: null, role: "Coach" },
];

function renderButton() {
  return render(
    <NextIntlClientProvider locale="en" messages={messages}>
      <NewCallButton personas={PERSONAS} />
    </NextIntlClientProvider>,
  );
}

describe("NewCallButton", () => {
  beforeEach(() => {
    h.push.mockReset();
    h.requestCall.mockReset();
    h.requestCall.mockReturnValue("started");
    h.post.mockClear();
  });

  it("labels the trigger 'New call'", () => {
    renderButton();
    expect(screen.getByLabelText("New call")).toBeInTheDocument();
  });

  it("mints an origin='call' conversation, requests the session, and navigates to /voice", async () => {
    renderButton();
    fireEvent.click(screen.getByLabelText("New call"));
    await waitFor(() => {
      expect(
        screen.getByRole("menuitem", { name: /Lena Brevik/ }),
      ).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole("menuitem", { name: /Lena Brevik/ }));

    await waitFor(() => expect(h.post).toHaveBeenCalledTimes(1));
    expect(h.post).toHaveBeenCalledWith(
      "/v1/personas/{persona_id}/conversations",
      expect.objectContaining({
        params: { path: { persona_id: "lena" } },
        body: { title: "", origin: "call" },
      }),
    );
    expect(h.requestCall).toHaveBeenCalledWith(
      expect.objectContaining({
        personaId: "lena",
        conversationId: "new-conv",
        personaName: "Lena Brevik",
        personaRole: "Coach",
      }),
    );
    expect(h.push).toHaveBeenCalledWith("/chat/new-conv/voice");
  });

  it("does NOT navigate when a switch confirm is pending (no bypass of the one-call rule)", async () => {
    h.requestCall.mockReturnValue("switch");
    renderButton();
    fireEvent.click(screen.getByLabelText("New call"));
    await waitFor(() => {
      expect(
        screen.getByRole("menuitem", { name: /Astrid Berg/ }),
      ).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole("menuitem", { name: /Astrid Berg/ }));
    await waitFor(() => expect(h.requestCall).toHaveBeenCalled());
    expect(h.push).not.toHaveBeenCalled();
  });
});
