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
import { DeleteAccountCard } from "./delete-account-card";

const apiPost = vi.fn();
const apiGet = vi.fn();
const PHRASE = "delete my account";
const signOut = vi.fn();
const replace = vi.fn();
let email: string | null = "Ann@Example.test";

vi.mock("@/lib/api/use-api", () => ({
  useApi: () => ({ POST: apiPost, GET: apiGet }),
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

/** The server's expected confirmation (review M5), as GET /v1/me/erase answers it. */
function expects(
  confirm: string,
  wallet: { plan_code: string; lots?: number[] } = { plan_code: "free" },
) {
  apiGet.mockImplementation((path: string) =>
    Promise.resolve({
      response: new Response(null, { status: 200 }),
      data:
        path === "/v1/me/wallet"
          ? {
              plan_code: wallet.plan_code,
              payg_lots: (wallet.lots ?? []).map((remaining) => ({
                credits_remaining: remaining,
                credits_total: 1000,
                expires_at: "2027-01-01T00:00:00Z",
              })),
            }
          : { confirm },
    }),
  );
}

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

async function openAndType(text: string) {
  fireEvent.click(slot("delete-account-start"));
  await waitFor(() => expect(apiGet).toHaveBeenCalledWith("/v1/me/erase"));
  await screen.findByText(/To confirm, type/);
  fireEvent.change(slot("delete-account-confirm"), {
    target: { value: text },
  });
}

afterEach(() => {
  apiPost.mockReset();
  apiGet.mockReset();
  signOut.mockReset();
  replace.mockReset();
  email = "Ann@Example.test";
});

describe("delete account card", () => {
  it("keeps the button off until the confirmation matches", async () => {
    expects("ann@example.test");
    renderCard();
    await openAndType("someone@else.test");

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
    expects("ann@example.test");
    renderCard();
    await openAndType("ann@example.test");

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
    expects("ann@example.test");
    renderCard();
    await openAndType("ann@example.test");

    fireEvent.click(slot("delete-account-submit"));

    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(signOut).not.toHaveBeenCalled();
    expect(replace).not.toHaveBeenCalled();
  });

  it("asks for what the server expects, not the provider's email", async () => {
    // Review M5: Clerk says Ann@Example.test, the stored account has no real email.
    expects(PHRASE);
    renderCard();
    await openAndType("ann@example.test");
    expect((slot("delete-account-submit") as HTMLButtonElement).disabled).toBe(
      true,
    );

    fireEvent.change(slot("delete-account-confirm"), {
      target: { value: PHRASE },
    });

    expect((slot("delete-account-submit") as HTMLButtonElement).disabled).toBe(
      false,
    );
  });

  it.each([
    ["a paid plan", { plan_code: "pro" }],
    ["prepaid credits left", { plan_code: "free", lots: [500] }],
  ])("says nothing is refunded for %s", async (_name, wallet) => {
    expects("ann@example.test", wallet);
    renderCard();
    await openAndType("");

    const line = await waitFor(() => {
      const found = slot("delete-account-refund");
      expect(found).not.toBeNull();
      return found;
    });
    expect(line.textContent).toBe(messages.settings.deleteAccountRefund);
    expect(line.textContent).toContain("Your plan ends now");
    expect(line.textContent).toContain("not refunded");
    expect(line.textContent).toContain("contact support before");
  });

  it("does not mention refunds to a free user with no prepaid credits", async () => {
    expects("ann@example.test", { plan_code: "free", lots: [0] });
    renderCard();
    await openAndType("");

    await waitFor(() => expect(apiGet).toHaveBeenCalledWith("/v1/me/wallet"));
    expect(slot("delete-account-refund")).toBeNull();
  });
});
