import { describe, expect, it } from "vitest";
import {
  rankPersonasByRecency,
  resolveCalls,
  resolveConversations,
  type SidebarCallInput,
  type SidebarConversationInput,
  type SidebarPersona,
} from "./sidebar-data";

const persona = (id: string, created_at: string): SidebarPersona => ({
  id,
  name: id.toUpperCase(),
  role: "role",
  created_at,
  avatar_url: null,
});

const convo = (
  id: string,
  persona_id: string,
  updated_at: string,
  extra: Partial<SidebarConversationInput> = {},
): SidebarConversationInput => ({
  id,
  persona_id,
  title: `t-${id}`,
  updated_at,
  // A "real" chat conversation by default: origin chat + ≥1 message.
  origin: "chat",
  last_message_role: "assistant",
  ...extra,
});

describe("rankPersonasByRecency", () => {
  it("orders used personas by conversation order, unused by created_at desc", () => {
    const personas = [
      persona("a", "2026-01-01"),
      persona("b", "2026-03-01"),
      persona("c", "2026-02-01"),
    ];
    // updated_at DESC already: c used most recently, then a.
    const conversations = [
      convo("1", "c", "2026-06-10"),
      convo("2", "a", "2026-06-09"),
      convo("3", "c", "2026-06-08"), // dup → ignored
    ];
    const ranked = rankPersonasByRecency(personas, conversations).map(
      (p) => p.id,
    );
    // used: c, a (first-seen order); unused: b (newest created).
    expect(ranked).toEqual(["c", "a", "b"]);
  });

  it("falls back entirely to created_at desc when no conversations exist", () => {
    const personas = [persona("a", "2026-01-01"), persona("b", "2026-03-01")];
    expect(rankPersonasByRecency(personas, []).map((p) => p.id)).toEqual([
      "b",
      "a",
    ]);
  });

  it("ignores conversations whose persona is missing", () => {
    const personas = [persona("a", "2026-01-01")];
    const ranked = rankPersonasByRecency(personas, [
      convo("1", "ghost", "2026-06-10"),
    ]);
    expect(ranked.map((p) => p.id)).toEqual(["a"]);
  });
});

describe("resolveConversations", () => {
  it("joins each conversation to its persona, preserving order", () => {
    const personas = [persona("a", "2026-01-01")];
    const rows = resolveConversations(
      [convo("1", "a", "2026-06-10"), convo("2", "ghost", "2026-06-09")],
      personas,
    );
    expect(rows).toHaveLength(2);
    expect(rows[0].persona?.id).toBe("a");
    expect(rows[1].persona).toBeNull();
    expect(rows.map((r) => r.id)).toEqual(["1", "2"]);
  });

  it("excludes call + empty conversations from the Messages list (R4 T4)", () => {
    const personas = [persona("a", "2026-01-01")];
    const rows = resolveConversations(
      [
        convo("real", "a", "2026-06-10"), // chat + has messages → kept
        convo("call", "a", "2026-06-09", { origin: "call" }), // call → excluded
        convo("empty", "a", "2026-06-08", { last_message_role: null }), // no messages → excluded
      ],
      personas,
    );
    expect(rows.map((r) => r.id)).toEqual(["real"]);
  });

  it("treats a conversation with no origin marker as chat (legacy rows) (R4 T4)", () => {
    const personas = [persona("a", "2026-01-01")];
    const rows = resolveConversations(
      [
        {
          id: "legacy",
          persona_id: "a",
          title: "t-legacy",
          updated_at: "2026-06-10",
          last_message_role: "user",
        },
      ],
      personas,
    );
    expect(rows.map((r) => r.id)).toEqual(["legacy"]);
  });
});

const call = (
  call_id: string,
  persona_id: string,
  duration_s: number | null | undefined,
): SidebarCallInput => ({
  call_id,
  conversation_id: `conv-${call_id}`,
  persona_id,
  started_at: "2026-06-10T00:00:00Z",
  duration_s,
});

describe("resolveCalls", () => {
  it("joins each call to its persona + the transcript conversation, preserving order", () => {
    const personas = [persona("a", "2026-01-01")];
    const rows = resolveCalls(
      [call("c1", "a", 125), call("c2", "ghost", 30)],
      personas,
    );
    expect(rows).toHaveLength(2);
    expect(rows[0].persona?.id).toBe("a");
    expect(rows[0].conversationId).toBe("conv-c1"); // the transcript link
    expect(rows[0].durationS).toBe(125);
    expect(rows[1].persona).toBeNull(); // missing persona → null (no crash)
    expect(rows.map((r) => r.callId)).toEqual(["c1", "c2"]);
  });

  it("normalises an absent duration (a live call) to null", () => {
    const rows = resolveCalls([call("c1", "a", undefined)], []);
    expect(rows[0].durationS).toBeNull();
  });
});
