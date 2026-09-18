/**
 * Issue #15: opening a conversation with history lands on the latest turn.
 *
 * jsdom has no layout, so these tests install a small scroll model on
 * `HTMLElement.prototype`: `scrollHeight` / `clientHeight` are read from a
 * mutable box, and every `scrollTop` write is recorded. That makes the three
 * rules in `use-chat-scroll-anchor.ts` assertable: where the view lands, and
 * (just as important) when nothing is allowed to move it.
 */
import { fireEvent, render } from "@testing-library/react";
import { act } from "react";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { useChatScrollAnchor } from "./use-chat-scroll-anchor";

/** The fake viewport: a 1000px log inside a 300px window. */
const box = { scrollHeight: 1000, clientHeight: 300 };
/** Every `scrollTop` the hook wrote, oldest first. */
let writes: number[] = [];
let scrollTop = 0;

/** Resize callbacks registered against the content element, newest last. */
let resizeCallbacks: Array<() => void> = [];

const original = {
  scrollHeight: Object.getOwnPropertyDescriptor(
    HTMLElement.prototype,
    "scrollHeight",
  ),
  clientHeight: Object.getOwnPropertyDescriptor(
    HTMLElement.prototype,
    "clientHeight",
  ),
  scrollTop: Object.getOwnPropertyDescriptor(
    HTMLElement.prototype,
    "scrollTop",
  ),
  resizeObserver: globalThis.ResizeObserver,
};

beforeAll(() => {
  Object.defineProperty(HTMLElement.prototype, "scrollHeight", {
    configurable: true,
    get(this: HTMLElement) {
      return this.hasAttribute("data-chat-scroller") ? box.scrollHeight : 0;
    },
  });
  Object.defineProperty(HTMLElement.prototype, "clientHeight", {
    configurable: true,
    get(this: HTMLElement) {
      return this.hasAttribute("data-chat-scroller") ? box.clientHeight : 0;
    },
  });
  Object.defineProperty(HTMLElement.prototype, "scrollTop", {
    configurable: true,
    get() {
      return scrollTop;
    },
    set(value: number) {
      scrollTop = value;
      writes.push(value);
    },
  });
  class TestResizeObserver {
    constructor(private readonly cb: () => void) {}
    observe() {
      resizeCallbacks.push(this.cb);
    }
    unobserve() {}
    disconnect() {
      resizeCallbacks = resizeCallbacks.filter((c) => c !== this.cb);
    }
  }
  globalThis.ResizeObserver =
    TestResizeObserver as unknown as typeof ResizeObserver;
});

afterAll(() => {
  for (const key of ["scrollHeight", "clientHeight", "scrollTop"] as const) {
    const descriptor = original[key];
    if (descriptor) {
      Object.defineProperty(HTMLElement.prototype, key, descriptor);
    } else {
      delete (HTMLElement.prototype as unknown as Record<string, unknown>)[key];
    }
  }
  globalThis.ResizeObserver = original.resizeObserver;
});

afterEach(() => {
  writes = [];
  scrollTop = 0;
  resizeCallbacks = [];
  box.scrollHeight = 1000;
  box.clientHeight = 300;
});

/** The scroller markup ChatWindow renders, driven by the hook under test. */
function Thread({
  conversationId,
  messages,
}: {
  conversationId: string;
  messages: readonly string[];
}) {
  const { scrollerRef, contentRef, onScroll } = useChatScrollAnchor(
    conversationId,
    messages,
  );
  return (
    <div
      ref={scrollerRef}
      onScroll={onScroll}
      data-chat-scroller=""
      data-testid="scroller"
    >
      <div ref={contentRef}>
        {messages.map((m) => (
          <p key={m}>{m}</p>
        ))}
      </div>
    </div>
  );
}

const turns = (n: number) =>
  Array.from({ length: n }, (_, i) => `turn ${i + 1}`);

/** Scroll the reader up on purpose and let the hook see it. */
function scrollUp(scroller: HTMLElement, to = 100) {
  scrollTop = to;
  writes = [];
  fireEvent.scroll(scroller);
}

/** Fire the observed content resize (a late image, an avatar, a chart). */
function contentResized() {
  act(() => {
    for (const cb of resizeCallbacks) cb();
  });
}

describe("useChatScrollAnchor", () => {
  it("opens a conversation with history on its latest turn", () => {
    render(<Thread conversationId="c1" messages={turns(12)} />);

    expect(writes.at(-1)).toBe(box.scrollHeight);
    expect(scrollTop).toBe(box.scrollHeight);
  });

  it("leaves an empty conversation exactly where it is", () => {
    // Nothing sent yet: the log is shorter than its viewport.
    box.scrollHeight = 200;

    render(<Thread conversationId="c1" messages={[]} />);
    contentResized();

    expect(writes).toEqual([]);
    expect(scrollTop).toBe(0);
  });

  it("does not move a reader who scrolled up when a message arrives", () => {
    const { getByTestId, rerender } = render(
      <Thread conversationId="c1" messages={turns(12)} />,
    );
    scrollUp(getByTestId("scroller"));

    box.scrollHeight = 1200;
    rerender(<Thread conversationId="c1" messages={turns(13)} />);

    expect(writes).toEqual([]);
    expect(scrollTop).toBe(100);
  });

  it("re-anchors to the bottom when late-loading content grows the log", () => {
    render(<Thread conversationId="c1" messages={turns(12)} />);
    writes = [];

    // An inline image finished fetching: same messages, taller log.
    box.scrollHeight = 1600;
    contentResized();

    expect(writes.at(-1)).toBe(1600);
  });

  it("keeps the anchor of a scrolled-up reader when the log grows above them", () => {
    const { getByTestId } = render(
      <Thread conversationId="c1" messages={turns(12)} />,
    );
    scrollUp(getByTestId("scroller"), 240);

    box.scrollHeight = 2000;
    contentResized();

    expect(writes).toEqual([]);
    expect(scrollTop).toBe(240);
  });

  it("opens at the latest turn again when the same conversation is reopened", () => {
    const first = render(<Thread conversationId="c1" messages={turns(12)} />);
    const firstLanding = writes.at(-1);
    first.unmount();
    writes = [];
    scrollTop = 0;

    render(<Thread conversationId="c1" messages={turns(12)} />);

    expect(writes.at(-1)).toBe(firstLanding);
    expect(scrollTop).toBe(box.scrollHeight);
  });

  it("re-anchors on a conversation switch even when the reader had scrolled up", () => {
    // The App Router REUSES ChatWindow across /chat/[id], so this is a rerender,
    // not a remount: without the conversation-keyed open effect the next
    // conversation would open halfway up its history.
    const { getByTestId, rerender } = render(
      <Thread conversationId="c1" messages={turns(12)} />,
    );
    scrollUp(getByTestId("scroller"));

    rerender(<Thread conversationId="c2" messages={turns(30)} />);

    expect(writes.at(-1)).toBe(box.scrollHeight);
  });

  it("follows new content for a reader who is still at the bottom", () => {
    const { rerender } = render(
      <Thread conversationId="c1" messages={turns(12)} />,
    );
    writes = [];

    box.scrollHeight = 1400;
    rerender(<Thread conversationId="c1" messages={turns(13)} />);

    expect(writes.at(-1)).toBe(1400);
  });
});
