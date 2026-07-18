/**
 * Spec K11 (T5, D-K11-7c) — the chat header's `remembers N` button.
 *
 * Renders as `role="button"` (not a real `<button>`, since this nests inside
 * the header's persona `<Link>` — see the component's own doc comment) and
 * opens the persona-scoped episodic manager on click AND on keyboard
 * (Enter/Space) without letting the click bubble to an ancestor link.
 *
 * `EpisodicManagerModal` is mocked, but its controlled `open` prop is
 * surfaced in the DOM so these tests exercise the real controlled-open
 * contract the component drives from `onClick`/`onKeyDown` — not a bypassed
 * mock click handler — so a keyboard-activation regression fails here.
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it, vi } from "vitest";
import type { AvatarPersona } from "@/components/persona/persona-avatar";
import { ChatRemembersButton } from "./chat-remembers-button";

vi.mock("@/components/memory/episodic-manager-modal", () => ({
  EpisodicManagerModal: ({
    personas,
    trigger,
    open,
  }: {
    personas: readonly AvatarPersona[];
    trigger?: React.ReactElement;
    open?: boolean;
    onOpenChange?: (open: boolean) => void;
  }) => (
    <div>
      <span data-testid="modal-personas">
        {personas.map((p) => p.name).join(", ")}
      </span>
      <span data-testid="modal-open">{String(open)}</span>
      <span data-testid="modal-trigger-present">{String(trigger != null)}</span>
      {trigger}
    </div>
  ),
}));

const messages = {
  chat: {
    remembers:
      "remembers {count, plural, one {# conversation} other {# conversations}}",
  },
};

const PERSONA: AvatarPersona = { id: "astrid", name: "Astrid Berg" };

describe("ChatRemembersButton", () => {
  it("renders the remembers-N label scoped to the conversation's persona", () => {
    render(
      <NextIntlClientProvider locale="en" messages={messages}>
        <ChatRemembersButton persona={PERSONA} count={3} />
      </NextIntlClientProvider>,
    );
    expect(screen.getByText("remembers 3 conversations")).toBeInTheDocument();
    expect(screen.getByTestId("modal-personas")).toHaveTextContent(
      "Astrid Berg",
    );
  });

  it("is keyboard + click accessible as role=button and stops the click from bubbling", () => {
    const onAncestorClick = vi.fn();
    render(
      <NextIntlClientProvider locale="en" messages={messages}>
        {/** biome-ignore lint/a11y/useKeyWithClickEvents: test harness stands in for the real ancestor <Link>. */}
        {/** biome-ignore lint/a11y/noStaticElementInteractions: test harness stands in for the real ancestor <Link>. */}
        <div onClick={onAncestorClick}>
          <ChatRemembersButton persona={PERSONA} count={1} />
        </div>
      </NextIntlClientProvider>,
    );
    const trigger = screen.getByRole("button", { name: /remembers/ });
    expect(trigger).toHaveAttribute("tabIndex", "0");
    fireEvent.click(trigger);
    expect(onAncestorClick).not.toHaveBeenCalled();
  });

  it("R9-054: does not feed a trigger element into EpisodicManagerModal (avoids the Dialog.Trigger nativeButton a11y warning)", () => {
    render(
      <NextIntlClientProvider locale="en" messages={messages}>
        <ChatRemembersButton persona={PERSONA} count={1} />
      </NextIntlClientProvider>,
    );
    // The span is self-driven (click/keydown → controlled open/onOpenChange
    // above); it must render as ChatRemembersButton's own markup, not get
    // routed through EpisodicManagerModal's `trigger` prop into a
    // Dialog.Trigger (which defaults nativeButton=true and warns on a
    // non-<button> host).
    expect(screen.getByTestId("modal-trigger-present")).toHaveTextContent(
      "false",
    );
    expect(
      screen.getByRole("button", { name: /remembers/ }),
    ).toBeInTheDocument();
  });

  it("opens the modal on click via the controlled open/onOpenChange pair", () => {
    render(
      <NextIntlClientProvider locale="en" messages={messages}>
        <ChatRemembersButton persona={PERSONA} count={1} />
      </NextIntlClientProvider>,
    );
    expect(screen.getByTestId("modal-open")).toHaveTextContent("false");
    fireEvent.click(screen.getByRole("button", { name: /remembers/ }));
    expect(screen.getByTestId("modal-open")).toHaveTextContent("true");
  });

  it("prevents the ancestor anchor's native navigation on click (K11-oracle regression)", () => {
    // jsdom doesn't perform real navigation, so the browser bug (click opens
    // the modal AND navigates via the ancestor <Link>) can't be observed by
    // watching location — it hid this from every prior unit test. What jsdom
    // DOES model correctly is whether preventDefault() was called:
    // fireEvent.click's return value is false iff the event was cancelled,
    // which is exactly what suppresses the anchor's default navigation.
    render(
      <NextIntlClientProvider locale="en" messages={messages}>
        <a href="/persona/astrid">
          <ChatRemembersButton persona={PERSONA} count={1} />
        </a>
      </NextIntlClientProvider>,
    );
    expect(screen.getByTestId("modal-open")).toHaveTextContent("false");
    const notCancelled = fireEvent.click(
      screen.getByRole("button", { name: /remembers/ }),
    );
    expect(notCancelled).toBe(false); // false means preventDefault() was called
    expect(screen.getByTestId("modal-open")).toHaveTextContent("true");
  });

  it("opens the modal on keyboard Enter, since role=button spans get no native keyboard activation", () => {
    render(
      <NextIntlClientProvider locale="en" messages={messages}>
        <ChatRemembersButton persona={PERSONA} count={1} />
      </NextIntlClientProvider>,
    );
    const trigger = screen.getByRole("button", { name: /remembers/ });
    expect(screen.getByTestId("modal-open")).toHaveTextContent("false");
    fireEvent.keyDown(trigger, { key: "Enter" });
    expect(screen.getByTestId("modal-open")).toHaveTextContent("true");
  });

  it("opens the modal on keyboard Space and prevents the page-scroll default", () => {
    render(
      <NextIntlClientProvider locale="en" messages={messages}>
        <ChatRemembersButton persona={PERSONA} count={1} />
      </NextIntlClientProvider>,
    );
    const trigger = screen.getByRole("button", { name: /remembers/ });
    expect(screen.getByTestId("modal-open")).toHaveTextContent("false");
    const notCancelled = fireEvent.keyDown(trigger, { key: " " });
    expect(notCancelled).toBe(false); // false means preventDefault() was called
    expect(screen.getByTestId("modal-open")).toHaveTextContent("true");
  });

  it("does not bubble the keyboard-open to the ancestor Link click handler", () => {
    const onAncestorClick = vi.fn();
    render(
      <NextIntlClientProvider locale="en" messages={messages}>
        {/** biome-ignore lint/a11y/useKeyWithClickEvents: test harness stands in for the real ancestor <Link>. */}
        {/** biome-ignore lint/a11y/noStaticElementInteractions: test harness stands in for the real ancestor <Link>. */}
        <div onClick={onAncestorClick}>
          <ChatRemembersButton persona={PERSONA} count={1} />
        </div>
      </NextIntlClientProvider>,
    );
    fireEvent.keyDown(screen.getByRole("button", { name: /remembers/ }), {
      key: "Enter",
    });
    expect(screen.getByTestId("modal-open")).toHaveTextContent("true");
    expect(onAncestorClick).not.toHaveBeenCalled();
  });
});
