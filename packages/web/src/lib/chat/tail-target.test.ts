import { describe, expect, it } from "vitest";
import { isLastAssistantMessage, isLastUserMessage } from "./tail-target";

/**
 * R9-025 leg C — tail-only rendering. Mirrors the api's `_tail_target` V1
 * scope exactly: regenerate (`isLastAssistantMessage`) is eligible ONLY on
 * the conversation's current last assistant message; edit
 * (`isLastUserMessage`) is eligible on the last user message, allowing for
 * its own trailing assistant reply — never an older turn.
 */

describe("isLastAssistantMessage", () => {
  it("true for the LAST message when it is an assistant reply", () => {
    const messages = [{ role: "user" }, { role: "assistant" }];
    expect(isLastAssistantMessage(messages, 1)).toBe(true);
  });

  it("false for an OLDER assistant reply (not the tail)", () => {
    const messages = [
      { role: "user" },
      { role: "assistant" }, // index 1 — older, not eligible
      { role: "user" },
      { role: "assistant" }, // index 3 — the tail
    ];
    expect(isLastAssistantMessage(messages, 1)).toBe(false);
    expect(isLastAssistantMessage(messages, 3)).toBe(true);
  });

  it("false when the last message is a user message (mid-turn / no reply yet)", () => {
    const messages = [
      { role: "user" },
      { role: "assistant" },
      { role: "user" },
    ];
    expect(isLastAssistantMessage(messages, 2)).toBe(false);
  });

  it("false for an empty list / out-of-range index", () => {
    expect(isLastAssistantMessage([], 0)).toBe(false);
    expect(isLastAssistantMessage([{ role: "assistant" }], 5)).toBe(false);
  });
});

describe("isLastUserMessage", () => {
  it("true for the LAST message when it is a lone user message (no reply yet)", () => {
    const messages = [
      { role: "user" },
      { role: "assistant" },
      { role: "user" },
    ];
    expect(isLastUserMessage(messages, 2)).toBe(true);
  });

  it("true for the SECOND-TO-LAST user message when the last is its own reply (the normal completed-turn shape)", () => {
    const messages = [{ role: "user" }, { role: "assistant" }];
    expect(isLastUserMessage(messages, 0)).toBe(true);
  });

  it("false for an OLDER user message (not the tail, even with a trailing pair further on)", () => {
    const messages = [
      { role: "user" }, // index 0 — older, not eligible
      { role: "assistant" },
      { role: "user" }, // index 2 — the tail
      { role: "assistant" },
    ];
    expect(isLastUserMessage(messages, 0)).toBe(false);
    expect(isLastUserMessage(messages, 2)).toBe(true);
  });

  it("false when the target is an assistant message", () => {
    const messages = [{ role: "user" }, { role: "assistant" }];
    expect(isLastUserMessage(messages, 1)).toBe(false);
  });

  it("false for an empty list / out-of-range index", () => {
    expect(isLastUserMessage([], 0)).toBe(false);
    expect(isLastUserMessage([{ role: "user" }], 5)).toBe(false);
  });
});
