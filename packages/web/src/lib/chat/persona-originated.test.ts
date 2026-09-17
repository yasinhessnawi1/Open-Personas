/**
 * Spec C0, the web half (part3 F11): a message the persona started renders LIVE as an
 * assistant bubble carrying the "started this" badge, and the same badge comes back on
 * reload from the persisted `originated` row. Before this, the event was parsed as
 * unknown and dropped, so a user watching the chat never saw the persona speak first.
 */
import { describe, expect, it } from "vitest";
import type { ChatMessageView } from "@/components/chat/message-element";
import { persistedToView, reduceChatEvent } from "@/lib/chat/reduce-chat-event";
import { chatSseToOutputContent } from "@/lib/normalisers/chat-output";
import { type ChatEvent, parseChatEvent } from "@/lib/sse-types";

const ORIGINATED: ChatEvent = {
  event: "persona_originated",
  data: {
    content: "I finished the tenancy summary you asked for.",
    persona_id: "astrid",
    persona_name: "Astrid",
    visual_ref: "",
    conversation_id: "conv_1",
  },
};

function freshTurn(): ChatMessageView {
  return { id: "a1", role: "assistant", content: "", events: [], tools: [] };
}

describe("persona_originated on the live chat stream", () => {
  it("is admitted by the parser instead of dropped as unknown", () => {
    const ev = parseChatEvent({
      event: "persona_originated",
      data: JSON.stringify(ORIGINATED.data),
    });

    expect(ev).not.toBeNull();
    expect(ev?.event).toBe("persona_originated");
  });

  it("renders a live assistant bubble that carries the badge", () => {
    const view = reduceChatEvent(freshTurn(), ORIGINATED);

    expect(view.content).toBe("I finished the tenancy summary you asked for.");
    expect(view.originated).toBe(true);
    expect(view.working).toBe(false);
    // The interleaved log keeps the text in stream order, like a chunk would.
    expect(view.events).toEqual([
      { kind: "text", delta: "I finished the tenancy summary you asked for." },
    ]);
  });

  it("is not output content: the classifier routes it to the bubble, not the dispatcher", () => {
    expect(chatSseToOutputContent(ORIGINATED)).toEqual([]);
  });
});

describe("persona_originated on a REOPENED conversation", () => {
  it("puts the badge back from the persisted originated row", () => {
    const view = persistedToView({
      id: "m1",
      role: "assistant",
      content: "I finished the tenancy summary you asked for.",
      originated: true,
    });

    expect(view.originated).toBe(true);
    expect(view.content).toBe("I finished the tenancy summary you asked for.");
  });

  it("leaves an ordinary reply byte-exact (no originated key at all)", () => {
    const view = persistedToView({
      id: "m2",
      role: "assistant",
      content: "Sure, here it is.",
      originated: false,
    });

    expect(view).toEqual({
      id: "m2",
      role: "assistant",
      content: "Sure, here it is.",
      tier: undefined,
      originated: undefined,
    });
    expect("originated" in view && view.originated).toBeFalsy();
  });
});
