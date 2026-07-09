/**
 * R9-014 (b) — new-message flow on the conversations page.
 *
 * Clicking "New message" opens the reusable persona picker; selecting a
 * persona calls the `startChat` server action (create conversation → redirect
 * to /chat/{id}) with that persona's id. The server action is mocked; the
 * navigation itself lives inside it (asserted by the library-card tests).
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { NewConversationButton } from "./new-conversation-button";

const startChat = vi.fn();
vi.mock("@/app/actions", () => ({
  startChat: (id: string) => startChat(id),
}));
vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ getToken: async () => null }),
}));

const messages = {
  conversations: { newMessage: "New message" },
  personaPicker: { choosePersona: "Choose a persona", empty: "None" },
};

const PERSONAS = [
  { id: "astrid", name: "Astrid Berg", avatar_url: null },
  { id: "lena", name: "Lena Brevik", avatar_url: null },
];

function renderButton() {
  return render(
    <NextIntlClientProvider locale="en" messages={messages}>
      <NewConversationButton personas={PERSONAS} />
    </NextIntlClientProvider>,
  );
}

describe("NewConversationButton", () => {
  beforeEach(() => startChat.mockReset());

  it("labels the trigger 'New message'", () => {
    renderButton();
    expect(screen.getByLabelText("New message")).toBeInTheDocument();
  });

  it("opens the picker and calls startChat with the chosen persona id", async () => {
    renderButton();
    fireEvent.click(screen.getByLabelText("New message"));
    await waitFor(() => {
      expect(
        screen.getByRole("menuitem", { name: /Lena Brevik/ }),
      ).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole("menuitem", { name: /Lena Brevik/ }));
    expect(startChat).toHaveBeenCalledWith("lena");
  });
});
