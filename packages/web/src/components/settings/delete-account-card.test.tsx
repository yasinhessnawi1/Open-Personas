/**
 * R9-284 T9: the delete-account card.
 *
 * 1. Nothing is sent until the user opens the form and types the confirmation the server
 *    expects (their email, or the phrase when there is no real email).
 * 2. A confirmed delete posts only the typed confirmation, then signs out and leaves for the
 *    public goodbye page.
 * 3. A refused delete stays signed in and says so.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it, vi } from "vitest";
import messages from "@/i18n/messages/en.json";
import {
  CONFIRM_PHRASE,
  confirmationFor,
  DeleteAccountCard,
} from "./delete-account-card";

const apiPost = vi.fn();
const signOut = vi.fn();
const replace = vi.fn();
let email: string | null = "Ann@Example.test";

vi.mock("@/lib/api/use-api", () => ({
  useApi: () => ({ POST: apiPost }),
}));

vi.mock("@/auth", () => ({
  useAccount: () => ({
    name: "",
    email,
    imageUrl: null,
    available: true,
    signOut,
    manageAccount: () => undefined,
  }),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace }),
}));

function renderCard() {
  return render(
    <NextIntlClientProvider locale="en" messages={messages}>
      <DeleteAccountCard />
    </NextIntlClientProvider>,
  );
}

function slot(name: string): HTMLElement {
  return document.querySelector(`[data-slot="${name}"]`) as HTMLElement;
}

function openAndType(text: string) {
  fireEvent.click(slot("delete-account-start"));
  fireEvent.change(slot("delete-account-confirm"), {
    target: { value: text },
  });
}

afterEach(() => {
  apiPost.mockReset();
  signOut.mockReset();
  replace.mockReset();
  email = "Ann@Example.test";
});

describe("confirmationFor", () => {
  it("is the email, trimmed and in lower case", () => {
    expect(confirmationFor(" Ann@Example.test ")).toBe("ann@example.test");
  });

  it("is the phrase without a real email", () => {
    expect(confirmationFor(null)).toBe(CONFIRM_PHRASE);
    expect(confirmationFor("user_2abc@users.noreply")).toBe(CONFIRM_PHRASE);
  });
});

describe("delete account card", () => {
  it("keeps the button off until the confirmation matches", () => {
    renderCard();
    openAndType("someone@else.test");

    expect((slot("delete-account-submit") as HTMLButtonElement).disabled).toBe(
      true,
    );
    fireEvent.submit(
      slot("delete-account-submit").closest("form") as HTMLFormElement,
    );
    expect(apiPost).not.toHaveBeenCalled();
  });

  it("posts the confirmation, signs out and leaves for the goodbye page", async () => {
    apiPost.mockResolvedValue({
      response: new Response(null, { status: 202 }),
      data: { erasure_id: "erasure_1", state: "pending" },
    });
    renderCard();
    openAndType("ann@example.test");

    fireEvent.click(slot("delete-account-submit"));

    await waitFor(() =>
      expect(replace).toHaveBeenCalledWith("/account-deleted"),
    );
    expect(apiPost).toHaveBeenCalledWith("/v1/me/erase", {
      body: { confirm: "ann@example.test" },
    });
    expect(signOut).toHaveBeenCalledOnce();
  });

  it("stays signed in and says so when the server refuses", async () => {
    apiPost.mockResolvedValue({
      response: new Response(null, { status: 422 }),
      error: { error: "account_erase_unconfirmed", detail: "no" },
    });
    renderCard();
    openAndType("ann@example.test");

    fireEvent.click(slot("delete-account-submit"));

    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(signOut).not.toHaveBeenCalled();
    expect(replace).not.toHaveBeenCalled();
  });

  it("asks for the phrase when the account has no real email", () => {
    email = null;
    renderCard();
    openAndType(CONFIRM_PHRASE);

    expect((slot("delete-account-submit") as HTMLButtonElement).disabled).toBe(
      false,
    );
  });
});
