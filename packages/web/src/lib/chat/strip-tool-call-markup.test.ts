import { describe, expect, it } from "vitest";
import { stripToolCallMarkup } from "./strip-tool-call-markup";

// Verbatim from production (GitHub issues #10 and #13).
const LEAK_PAREN =
  "Let me check what's actually on the books rather than trusting memory.\n" +
  '<tool_call>schedule_introspect(scope: "all", days_ahead: 7)' +
  "The schedule is in good order:";
const LEAK_MANGLED =
  'Let me check the clock so we know exactly when "an hour from now" is.' +
  '<tool_call>datetime tool_call: </arg_value><arg_key>tool": "mcp_search", ' +
  '"args": {"query": "schedule a one-time task or reminder", "top_k": 5}}';

describe("stripToolCallMarkup", () => {
  it("removes the call-signature leak and keeps the prose on both sides", () => {
    const out = stripToolCallMarkup(LEAK_PAREN);
    expect(out).not.toContain("<tool_call>");
    expect(out).not.toContain("schedule_introspect(");
    expect(out).toContain("rather than trusting memory");
    expect(out).toContain("The schedule is in good order:");
  });

  it("removes the mangled arg-tag leak entirely", () => {
    const out = stripToolCallMarkup(LEAK_MANGLED);
    expect(out).not.toContain("<tool_call>");
    expect(out).not.toContain("<arg_key>");
    expect(out).not.toContain("</arg_value>");
    expect(out).not.toContain("mcp_search");
    expect(out).toContain('exactly when "an hour from now" is.');
  });

  it("removes a closed GLM tag block", () => {
    const out = stripToolCallMarkup(
      "One moment.\n<tool_call>datetime\n<arg_key>timezone</arg_key>\n" +
        '<arg_value>"Europe/Oslo"</arg_value>\n</tool_call>\nThere we go.',
    );
    expect(out).not.toContain("<");
    expect(out).toContain("One moment.");
    expect(out).toContain("There we go.");
  });

  it("removes orphan arg tags with no opener", () => {
    expect(
      stripToolCallMarkup("Sure.</arg_value><arg_key>x</arg_key> Done."),
    ).toBe("Sure. Done.");
  });

  it("hides a half-arrived opening tag instead of flashing it", () => {
    expect(stripToolCallMarkup("Checking the books.<tool_c")).toBe(
      "Checking the books.",
    );
  });

  it("leaves ordinary prose alone", () => {
    const prose = "No markup here. 3 < 4, and a <b> tag-ish thing.";
    expect(stripToolCallMarkup(prose)).toBe(prose);
    expect(stripToolCallMarkup("")).toBe("");
  });

  it("bounds an unterminated fragment to its own paragraph", () => {
    const out = stripToolCallMarkup(
      "First.\n<tool_call>garbage garbage garbage\n\nSecond paragraph survives.",
    );
    expect(out).not.toContain("garbage");
    expect(out).toContain("First.");
    expect(out).toContain("Second paragraph survives.");
  });
});
