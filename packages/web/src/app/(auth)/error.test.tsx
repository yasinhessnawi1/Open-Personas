/**
 * Spec 34 (black-screen hardening) — (auth) error-boundary fallback test.
 *
 * `error.tsx` is the fallback Next renders when a child of the `(auth)` segment
 * throws during render. This asserts the fallback renders branded, actionable
 * recovery (a `reset()`-wired "Try again" + a hard link back to /sign-in)
 * instead of the blank screen a thrown render error used to leave behind — and,
 * via a tiny boundary harness, that a throwing child surfaces the fallback
 * rather than propagating the crash.
 */
import { render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { Component, type ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import messages from "@/i18n/messages/en.json";
import AuthError from "./error";

/** The boundary copy now lives in en.json; render it the way the app does. */
function withIntl(node: ReactNode) {
  return (
    <NextIntlClientProvider locale="en" messages={messages}>
      {node}
    </NextIntlClientProvider>
  );
}

describe("(auth) error boundary fallback", () => {
  it("renders the branded fallback with a reset action and a sign-in link", () => {
    const reset = vi.fn();
    render(withIntl(<AuthError error={new Error("boom")} reset={reset} />));

    expect(
      screen.getByRole("heading", { name: messages.authError.title }),
    ).toBeInTheDocument();
    expect(screen.getByRole("alert")).toBeInTheDocument();

    const tryAgain = screen.getByRole("button", {
      name: messages.authError.retry,
    });
    tryAgain.click();
    expect(reset).toHaveBeenCalledOnce();

    const back = screen.getByRole("link", {
      name: messages.authError.backToSignIn,
    });
    expect(back).toHaveAttribute("href", "/sign-in");
  });

  it("surfaces the fallback when a child throws (defense-in-depth)", () => {
    // A minimal class boundary that renders AuthError as its fallback — mirrors
    // how Next wires error.tsx under the segment, proving a render throw turns
    // into the fallback rather than a blank tree.
    class Boundary extends Component<
      { children: ReactNode },
      { error: Error | null }
    > {
      state = { error: null as Error | null };
      static getDerivedStateFromError(error: Error) {
        return { error };
      }
      reset = () => this.setState({ error: null });
      render() {
        if (this.state.error) {
          return <AuthError error={this.state.error} reset={this.reset} />;
        }
        return this.props.children;
      }
    }

    function Boom(): ReactNode {
      throw new Error("render crash");
    }

    // jsdom prints the caught error; silence it so the run stays clean.
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    try {
      expect(() =>
        render(
          withIntl(
            <Boundary>
              <Boom />
            </Boundary>,
          ),
        ),
      ).not.toThrow();
      expect(
        screen.getByRole("heading", { name: messages.authError.title }),
      ).toBeInTheDocument();
    } finally {
      spy.mockRestore();
    }
  });
});
