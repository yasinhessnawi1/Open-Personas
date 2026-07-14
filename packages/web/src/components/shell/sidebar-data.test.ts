import { describe, expect, it } from "vitest";
import {
  buildConversationPersonaMap,
  derivePersonaRoutePresence,
  rankPersonasByRecency,
  resolveCalls,
  resolveConversations,
  resolvePersonaPresence,
  type SidebarCall,
  type SidebarCallInput,
  type SidebarConversation,
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

const astrid = persona("astrid", "2026-01-01");

const convoRow = (
  id: string,
  p: SidebarPersona | null,
): SidebarConversation => ({
  id,
  title: `t-${id}`,
  updated_at: "2026-06-10T00:00:00Z",
  persona: p,
});

const callRow = (
  callId: string,
  conversationId: string,
  p: SidebarPersona | null,
): SidebarCall => ({
  callId,
  conversationId,
  startedAt: "2026-06-10T00:00:00Z",
  durationS: null,
  persona: p,
});

describe("buildConversationPersonaMap (R9-038)", () => {
  it("maps chat conversation ids AND call conversation ids to their persona id", () => {
    const map = buildConversationPersonaMap(
      [convoRow("chat-1", astrid)],
      [callRow("call_1", "call-conv-1", astrid)],
    );
    expect(map.get("chat-1")).toBe("astrid");
    expect(map.get("call-conv-1")).toBe("astrid");
    expect(map.size).toBe(2);
  });

  it("skips rows with no resolved persona (never crashes, never maps to a ghost id)", () => {
    const map = buildConversationPersonaMap(
      [convoRow("orphan", null)],
      [callRow("call_1", "orphan-call", null)],
    );
    expect(map.size).toBe(0);
  });
});

describe("derivePersonaRoutePresence (R9-038)", () => {
  const map = buildConversationPersonaMap(
    [convoRow("c1", astrid)],
    [callRow("call_1", "call-c1", astrid)],
  );

  it("/chat/{id} resolves to in-chat (activePersonaId), not on-call", () => {
    expect(derivePersonaRoutePresence("/chat/c1", map)).toEqual({
      activePersonaId: "astrid",
      onCallPersonaId: null,
    });
  });

  it("/chat/{id}/voice resolves to on-call, not in-chat", () => {
    expect(derivePersonaRoutePresence("/chat/call-c1/voice", map)).toEqual({
      activePersonaId: null,
      onCallPersonaId: "astrid",
    });
  });

  it("tolerates a trailing slash on both forms", () => {
    expect(derivePersonaRoutePresence("/chat/c1/", map)).toEqual({
      activePersonaId: "astrid",
      onCallPersonaId: null,
    });
    expect(derivePersonaRoutePresence("/chat/call-c1/voice/", map)).toEqual({
      activePersonaId: null,
      onCallPersonaId: "astrid",
    });
  });

  it("/personas/{id} resolves to in-chat (the persona page counts as 'using' it)", () => {
    expect(derivePersonaRoutePresence("/personas/astrid", map)).toEqual({
      activePersonaId: "astrid",
      onCallPersonaId: null,
    });
  });

  it("nested persona subpaths (edit/files) still resolve to in-chat", () => {
    expect(derivePersonaRoutePresence("/personas/astrid/edit", map)).toEqual({
      activePersonaId: "astrid",
      onCallPersonaId: null,
    });
    expect(derivePersonaRoutePresence("/personas/astrid/files", map)).toEqual({
      activePersonaId: "astrid",
      onCallPersonaId: null,
    });
  });

  it("/personas/new is excluded (no persona id yet)", () => {
    expect(derivePersonaRoutePresence("/personas/new", map)).toEqual({
      activePersonaId: null,
      onCallPersonaId: null,
    });
  });

  it("a conversation id outside the sidebar's own map resolves to no presence", () => {
    expect(derivePersonaRoutePresence("/chat/never-fetched", map)).toEqual({
      activePersonaId: null,
      onCallPersonaId: null,
    });
  });

  it("unrelated routes resolve to no presence", () => {
    expect(derivePersonaRoutePresence("/", map)).toEqual({
      activePersonaId: null,
      onCallPersonaId: null,
    });
    expect(derivePersonaRoutePresence("/calls", map)).toEqual({
      activePersonaId: null,
      onCallPersonaId: null,
    });
  });
});

describe("resolvePersonaPresence (R9-038)", () => {
  it("no signal → 'none', no secondary dot", () => {
    expect(
      resolvePersonaPresence({
        isOnCall: false,
        isActiveChat: false,
        isWorking: false,
      }),
    ).toEqual({ presence: "none", secondaryWorking: false });
  });

  it("working alone → 'working' ring, no secondary dot (nothing to be secondary to)", () => {
    expect(
      resolvePersonaPresence({
        isOnCall: false,
        isActiveChat: false,
        isWorking: true,
      }),
    ).toEqual({ presence: "working", secondaryWorking: false });
  });

  it("in-chat alone → 'chat' ring, no secondary dot", () => {
    expect(
      resolvePersonaPresence({
        isOnCall: false,
        isActiveChat: true,
        isWorking: false,
      }),
    ).toEqual({ presence: "chat", secondaryWorking: false });
  });

  it("in-chat + working → 'chat' ring PLUS the secondary dot (never a second ring)", () => {
    expect(
      resolvePersonaPresence({
        isOnCall: false,
        isActiveChat: true,
        isWorking: true,
      }),
    ).toEqual({ presence: "chat", secondaryWorking: true });
  });

  it("on-call alone → 'call' ring, no secondary dot", () => {
    expect(
      resolvePersonaPresence({
        isOnCall: true,
        isActiveChat: false,
        isWorking: false,
      }),
    ).toEqual({ presence: "call", secondaryWorking: false });
  });

  it("on-call + working → 'call' ring PLUS the secondary dot", () => {
    expect(
      resolvePersonaPresence({
        isOnCall: true,
        isActiveChat: false,
        isWorking: true,
      }),
    ).toEqual({ presence: "call", secondaryWorking: true });
  });

  it("priority: on-call beats in-chat even if both are somehow true", () => {
    expect(
      resolvePersonaPresence({
        isOnCall: true,
        isActiveChat: true,
        isWorking: false,
      }),
    ).toEqual({ presence: "call", secondaryWorking: false });
  });
});
