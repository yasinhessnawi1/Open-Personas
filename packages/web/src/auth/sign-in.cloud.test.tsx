/**
 * The custom sign-in must finish every status Clerk can return after a correct
 * password (R9-136). Clerk asks a browser it has not seen before to prove the
 * email address (`needs_client_trust`) and verifies that exactly like a second
 * factor. The form used to handle only `complete` and `needs_second_factor`, so a
 * correct password on a new device ended in "Something went wrong" with nothing in
 * the console. These drive the real component through its real steps.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import messages from "@/i18n/messages/en.json";

const useSignInMock = vi.fn();
const useAuthMock = vi.fn(() => ({ isLoaded: true, isSignedIn: false }));
vi.mock("@clerk/nextjs", () => ({
  useSignIn: () => useSignInMock(),
  useAuth: () => useAuthMock(),
}));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

import { SignIn } from "./sign-in.cloud";

type Status =
  | "needs_identifier"
  | "needs_first_factor"
  | "needs_second_factor"
  | "needs_client_trust"
  | "needs_new_password"
  | "complete";

/** A scripted sign-in resource: `afterPassword` is the status Clerk returns. */
function fakeSignIn(afterPassword: Status) {
  const calls: string[] = [];
  const resource = {
    status: "needs_identifier" as Status,
    identifier: null as string | null,
    async create({ identifier }: { identifier: string }) {
      calls.push("create");
      resource.identifier = identifier;
      resource.status = "needs_first_factor";
      return { error: null };
    },
    async password() {
      calls.push("password");
      resource.status = afterPassword;
      return { error: null };
    },
    mfa: {
      async sendEmailCode() {
        calls.push("sendEmailCode");
        return { error: null };
      },
      async verifyEmailCode() {
        calls.push("verifyEmailCode");
        resource.status = "complete";
        return { error: null };
      },
    },
    async finalize() {
      calls.push("finalize");
      return { error: null };
    },
    async reset() {
      calls.push("reset");
      return { error: null };
    },
    async sso() {
      return { error: null };
    },
  };
  return { resource, calls };
}

function wrap(children: ReactNode) {
  return (
    <NextIntlClientProvider locale="en" messages={messages}>
      {children}
    </NextIntlClientProvider>
  );
}

async function signInWith(password: string) {
  fireEvent.change(screen.getByLabelText("Email"), {
    target: { value: "person@example.com" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Continue" }));
  await screen.findByLabelText("Password");
  fireEvent.change(screen.getByLabelText("Password"), {
    target: { value: password },
  });
  fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
}

describe("custom sign-in after a correct password", () => {
  afterEach(() => vi.restoreAllMocks());

  it("finishes a new-device sign-in through the email code, and only then", async () => {
    const { resource, calls } = fakeSignIn("needs_client_trust");
    useSignInMock.mockReturnValue({
      signIn: resource,
      errors: { fields: {} },
      fetchStatus: "idle",
    });
    render(wrap(<SignIn />));

    await signInWith("correct horse battery staple");

    // THE regression: the status must move the form to the code step, not to the
    // generic error, and Clerk must have been asked to send the code.
    await screen.findByRole("heading", { name: "Verify it's you" });
    expect(calls).toEqual(["create", "password", "sendEmailCode"]);
    expect(screen.queryByText(/Something went wrong/)).toBeNull();

    // Six digits into the code boxes completes the sign-in.
    const boxes = screen
      .getAllByRole("textbox")
      .filter((el) => el.getAttribute("inputmode") === "numeric");
    expect(boxes).toHaveLength(6);
    for (const [i, d] of Array.from("123456").entries()) {
      fireEvent.change(boxes[i], { target: { value: d } });
    }
    await waitFor(() => expect(calls).toContain("finalize"));
    expect(calls.slice(-2)).toEqual(["verifyEmailCode", "finalize"]);
  });

  it("a known device still signs straight in without a code", async () => {
    const { resource, calls } = fakeSignIn("complete");
    useSignInMock.mockReturnValue({
      signIn: resource,
      errors: { fields: {} },
      fetchStatus: "idle",
    });
    render(wrap(<SignIn />));

    await signInWith("correct horse battery staple");

    await waitFor(() => expect(calls).toContain("finalize"));
    expect(calls).not.toContain("sendEmailCode");
  });

  it("a status the flow does not handle is named in the console, not swallowed", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const { resource } = fakeSignIn("needs_new_password");
    useSignInMock.mockReturnValue({
      signIn: resource,
      errors: { fields: {} },
      fetchStatus: "idle",
    });
    render(wrap(<SignIn />));

    await signInWith("correct horse battery staple");

    await screen.findByText(/Something went wrong/);
    expect(warn).toHaveBeenCalledWith(
      expect.stringContaining("unhandled status"),
      "needs_new_password",
    );
  });
});
