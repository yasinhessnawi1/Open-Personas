/**
 * Spec 34 (black-screen hardening) — readiness-guard tests.
 *
 * Reproduces the post-logout crash: the Core-3 signal hooks `useSignIn()` /
 * `useSignUp()` are TYPED non-null, but during the client reset window they
 * transiently return `{ signIn: null, errors: undefined }`. Reading
 * `errors.fields` (or `signIn.*`) in that window threw and blanked the screen.
 *
 * These tests mock the hooks to the not-ready shape and assert each branded
 * component renders the calm loading state (the brand shell + spinner) INSTEAD
 * of throwing — and still renders the real form once the signal is ready.
 */
import { render as rtlRender, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import messages from "@/i18n/messages/en.json";
import { isAuthSignalReady } from "./auth-ready.cloud";

/** The branded auth copy lives in `auth.*`; render it the way the app does. */
const render = (ui: ReactNode) =>
  rtlRender(
    <NextIntlClientProvider locale="en" messages={messages}>
      {ui}
    </NextIntlClientProvider>,
  );

// --- Clerk hook + router mocks ----------------------------------------------
// The flow components import these from `@clerk/nextjs`; the test controls what
// the signal hooks return so we can drive the not-ready / ready shapes.
const useSignInMock = vi.fn();
const useSignUpMock = vi.fn();
// SignIn / SignUp also call `useAuth()` (already-signed-in redirect guard).
// Default to a loaded, signed-out session so the guard never fires and the form
// renders — these tests assert the post-logout reset / ready-signal branches.
const useAuthMock = vi.fn(() => ({ isLoaded: true, isSignedIn: false }));

vi.mock("@clerk/nextjs", () => ({
  useSignIn: () => useSignInMock(),
  useSignUp: () => useSignUpMock(),
  useAuth: () => useAuthMock(),
}));

const replaceMock = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: replaceMock }),
}));

// Imported AFTER the mocks are registered.
import { ResetPassword } from "./reset-password.cloud";
import { SignIn } from "./sign-in.cloud";
import { SignUp } from "./sign-up.cloud";

/** The not-ready shape the hooks return during the post-logout client reset. */
const NOT_READY_SIGN_IN = {
  signIn: null,
  errors: undefined,
  fetchStatus: "idle",
};
const NOT_READY_SIGN_UP = {
  signUp: null,
  errors: undefined,
  fetchStatus: "idle",
};

/** A minimal ready sign-in signal (errors.fields present, resource present). */
const READY_SIGN_IN = {
  signIn: { status: "needs_identifier", identifier: null },
  errors: {
    fields: { identifier: null, password: null, code: null },
    global: null,
    raw: null,
  },
  fetchStatus: "idle",
};
const READY_SIGN_UP = {
  signUp: { status: "missing_requirements", emailAddress: null },
  errors: {
    fields: { emailAddress: null, password: null, code: null },
    global: null,
    raw: null,
  },
  fetchStatus: "idle",
};

beforeEach(() => {
  useSignInMock.mockReset();
  useSignUpMock.mockReset();
  useAuthMock.mockReset();
  useAuthMock.mockReturnValue({ isLoaded: true, isSignedIn: false });
  replaceMock.mockReset();
});

describe("isAuthSignalReady", () => {
  it("is false when the resource handle is null (reset window)", () => {
    expect(isAuthSignalReady({ resource: null, errors: { fields: {} } })).toBe(
      false,
    );
  });

  it("is false when errors is undefined (reset window)", () => {
    expect(isAuthSignalReady({ resource: {}, errors: undefined })).toBe(false);
  });

  it("is false when errors.fields is missing", () => {
    expect(isAuthSignalReady({ resource: {}, errors: {} })).toBe(false);
  });

  it("is true when both the resource and errors.fields are present", () => {
    expect(isAuthSignalReady({ resource: {}, errors: { fields: {} } })).toBe(
      true,
    );
  });
});

describe("SignIn — post-logout reset window", () => {
  it("renders the loading state, not a crash, when the signal is not ready", () => {
    useSignInMock.mockReturnValue(NOT_READY_SIGN_IN);
    expect(() => render(<SignIn />)).not.toThrow();
    expect(screen.getByRole("status")).toBeInTheDocument();
    // Brand shell is still present (calm loading, never a blank canvas).
    expect(screen.getAllByAltText("Open Persona").length).toBeGreaterThan(0);
  });

  it("renders the real form once the signal is ready", () => {
    useSignInMock.mockReturnValue(READY_SIGN_IN);
    render(<SignIn />);
    expect(
      screen.getByRole("heading", { name: "Welcome back" }),
    ).toBeInTheDocument();
  });
});

describe("already-signed-in guard (session_exists fix)", () => {
  it("SignIn redirects away (no form) when a session is already active", () => {
    useSignInMock.mockReturnValue(READY_SIGN_IN);
    useAuthMock.mockReturnValue({ isLoaded: true, isSignedIn: true });
    render(<SignIn />);
    // The configured app target is replaced into history…
    expect(replaceMock).toHaveBeenCalledWith("/personas");
    // …and the sign-in form is NOT rendered (calm loading shown instead).
    expect(
      screen.queryByRole("heading", { name: "Welcome back" }),
    ).not.toBeInTheDocument();
  });

  it("SignUp redirects away (no form) when a session is already active", () => {
    useSignUpMock.mockReturnValue(READY_SIGN_UP);
    useAuthMock.mockReturnValue({ isLoaded: true, isSignedIn: true });
    render(<SignUp />);
    expect(replaceMock).toHaveBeenCalledWith("/personas");
    expect(
      screen.queryByRole("heading", { name: "Create your account" }),
    ).not.toBeInTheDocument();
  });

  it("SignIn renders the form when loaded but signed out", () => {
    useSignInMock.mockReturnValue(READY_SIGN_IN);
    useAuthMock.mockReturnValue({ isLoaded: true, isSignedIn: false });
    render(<SignIn />);
    expect(replaceMock).not.toHaveBeenCalled();
    expect(
      screen.getByRole("heading", { name: "Welcome back" }),
    ).toBeInTheDocument();
  });
});

describe("SignUp — post-logout reset window", () => {
  it("renders the loading state, not a crash, when the signal is not ready", () => {
    useSignUpMock.mockReturnValue(NOT_READY_SIGN_UP);
    expect(() => render(<SignUp />)).not.toThrow();
    expect(screen.getByRole("status")).toBeInTheDocument();
  });

  it("renders the real form once the signal is ready", () => {
    useSignUpMock.mockReturnValue(READY_SIGN_UP);
    render(<SignUp />);
    expect(
      screen.getByRole("heading", { name: "Create your account" }),
    ).toBeInTheDocument();
  });
});

describe("ResetPassword — post-logout reset window", () => {
  it("renders the loading state, not a crash, when the signal is not ready", () => {
    useSignInMock.mockReturnValue(NOT_READY_SIGN_IN);
    expect(() => render(<ResetPassword />)).not.toThrow();
    expect(screen.getByRole("status")).toBeInTheDocument();
  });

  it("renders the real form once the signal is ready", () => {
    useSignInMock.mockReturnValue(READY_SIGN_IN);
    render(<ResetPassword />);
    expect(
      screen.getByRole("heading", { name: "Forgot your password?" }),
    ).toBeInTheDocument();
  });
});
