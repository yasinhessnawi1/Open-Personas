import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useConversationDocuments } from "./use-conversation-documents";

// A stable getToken, as Clerk's is: a new function each render would re-create
// `refresh` and re-fetch on every render.
const auth = vi.hoisted(() => ({
  getToken: () => Promise.resolve("jwt-token"),
}));
vi.mock("@clerk/nextjs", () => ({
  useAuth: () => auth,
}));

const meHandlers = new Map<string, () => void>();
vi.mock("@/components/providers/me-events-provider", () => ({
  useMeEvent: (type: string, handler: () => void) => {
    meHandlers.set(type, handler);
  },
}));

const FAKE_DOC = {
  doc_ref: "report-1",
  filename: "report.pdf",
  title: "report.pdf",
  format: "pdf",
  workspace_path: "persona_a/conversations/conv_1/documents/report-1.pdf",
  strategy: "whole_inject" as const,
  token_count: 800,
  page_count: 5,
  sheet_names: null,
  size_bytes: 20_000,
  images: [],
  text_stored: false,
  text_partial: false,
  index_state: "not_needed" as const,
  chunks_indexed: null,
};

const FAKE_DOC_2 = {
  ...FAKE_DOC,
  doc_ref: "memo-1",
  filename: "memo.docx",
  format: "docx",
};

describe("useConversationDocuments", () => {
  beforeEach(() => {
    globalThis.fetch = vi.fn(async () => {
      return new Response(JSON.stringify([FAKE_DOC]), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }) as unknown as typeof fetch;
  });
  afterEach(() => vi.restoreAllMocks());

  it("fetches on mount and exposes documents", async () => {
    const { result } = renderHook(() => useConversationDocuments("conv_1"));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.documents).toHaveLength(1);
    expect(result.current.documents[0].doc_ref).toBe("report-1");
    expect(result.current.error).toBeNull();
  });

  it("addOptimistic appends without dedup-collision", async () => {
    const { result } = renderHook(() => useConversationDocuments("conv_1"));
    await waitFor(() => expect(result.current.loading).toBe(false));
    act(() => result.current.addOptimistic(FAKE_DOC_2));
    expect(result.current.documents).toHaveLength(2);
    expect(result.current.documents.map((d) => d.doc_ref)).toEqual([
      "report-1",
      "memo-1",
    ]);
  });

  it("addOptimistic deduplicates on doc_ref collision (no double-add)", async () => {
    const { result } = renderHook(() => useConversationDocuments("conv_1"));
    await waitFor(() => expect(result.current.loading).toBe(false));
    act(() => result.current.addOptimistic(FAKE_DOC)); // already in list
    expect(result.current.documents).toHaveLength(1);
  });

  it("removeOptimistic drops the matching ref", async () => {
    const { result } = renderHook(() => useConversationDocuments("conv_1"));
    await waitFor(() => expect(result.current.loading).toBe(false));
    act(() => result.current.removeOptimistic("report-1"));
    expect(result.current.documents).toHaveLength(0);
  });

  it("refresh re-fetches and overwrites local state", async () => {
    const { result } = renderHook(() => useConversationDocuments("conv_1"));
    await waitFor(() => expect(result.current.loading).toBe(false));
    // Local optimistic ADD that the server doesn't know about; refresh
    // should overwrite the list with the server's view (just [FAKE_DOC]).
    act(() => result.current.addOptimistic(FAKE_DOC_2));
    expect(result.current.documents).toHaveLength(2);
    void result.current.refresh();
    await waitFor(() => expect(result.current.documents).toHaveLength(1));
  });

  it("sets error on 5xx", async () => {
    globalThis.fetch = vi.fn(
      async () => new Response("oops", { status: 503 }),
    ) as unknown as typeof fetch;
    const { result } = renderHook(() => useConversationDocuments("conv_1"));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.error).not.toBeNull();
  });

  it("re-fetches when conversationId changes (the sole dep — D-F3-X-cap-attached-state-on-conversation-switch)", async () => {
    const fetchSpy = globalThis.fetch as ReturnType<typeof vi.fn>;
    const { rerender, result } = renderHook(
      ({ id }: { id: string }) => useConversationDocuments(id),
      { initialProps: { id: "conv_1" } },
    );
    await waitFor(() => expect(result.current.loading).toBe(false));
    const callsBefore = fetchSpy.mock.calls.length;

    rerender({ id: "conv_2" });
    await waitFor(() =>
      expect(fetchSpy.mock.calls.length).toBeGreaterThan(callsBefore),
    );
    // openapi-fetch may pass a Request object; check its URL property if so.
    const lastCall = fetchSpy.mock.calls.at(-1) ?? [""];
    const arg = lastCall[0];
    const url =
      typeof arg === "string"
        ? arg
        : arg instanceof Request
          ? arg.url
          : String(arg);
    expect(url).toContain("conv_2");
  });

  it("refreshes when the document index pings sidebar.changed (D-FV0-31)", async () => {
    const { result } = renderHook(() => useConversationDocuments("conv_1"));
    await waitFor(() => expect(result.current.loading).toBe(false));
    const calls = (
      globalThis.fetch as unknown as { mock: { calls: unknown[] } }
    ).mock.calls.length;
    act(() => meHandlers.get("sidebar.changed")?.());
    await waitFor(() =>
      expect(
        (globalThis.fetch as unknown as { mock: { calls: unknown[] } }).mock
          .calls.length,
      ).toBe(calls + 1),
    );
  });
});
