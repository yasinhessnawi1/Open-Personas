/**
 * Spec W1 (T8 fold-in) — the nav-counts MAPPING, not just the badge that renders it.
 *
 * `sidebar.test.tsx` pins the body: given counts, the Activity row shows `attention`. It feeds
 * those counts from a fixture, so it cannot see the wire between the API's payload and that
 * fixture's shape. A mutation that mapped `attention` from `active_tasks` left every one of
 * those tests green and would have shipped a badge that lies about what needs you (R9-099, the
 * exact failure D-W1-5 exists to end).
 *
 * So this pins the seam itself, with a payload whose two counts are deliberately different:
 * whatever `attention` ends up being, it is the API's `attention` and nothing else.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

import { EMPTY_NAV_COUNTS } from "./sidebar-data";
import { fetchSidebarData } from "./sidebar-fetch";

const api = vi.hoisted(() => ({ GET: vi.fn() }));
vi.mock("@/lib/api/server", () => ({ serverApi: () => Promise.resolve(api) }));

/** The one payload shape that matters here: every count distinct, so no two can be confused. */
const NAV_COUNTS = {
  personas: 12,
  conversations: 34,
  calls: 5,
  memory_nodes: 7,
  active_tasks: 2,
  attention: 9,
  schedules: 3,
};

function respond(path: string) {
  if (path === "/v1/me/nav-counts") return { data: NAV_COUNTS };
  if (path === "/v1/memory/graph") return { data: { available: true } };
  if (path === "/v1/me/profile") return { data: { first_name: "Yasin" } };
  return { data: [] };
}

beforeEach(() => {
  vi.clearAllMocks();
  api.GET.mockImplementation((path: string) => Promise.resolve(respond(path)));
});

describe("the nav counts the sidebar renders come from the matching API fields", () => {
  it("maps attention from attention, never from the active working set (D-W1-5)", async () => {
    const { counts } = await fetchSidebarData();
    expect(counts.attention).toBe(9); // the API's `attention`
    expect(counts.activeTasks).toBe(2); // the working set, kept and served, badge-irrelevant
    expect(counts.attention).not.toBe(counts.activeTasks);
  });

  it("maps every other count from its own field", async () => {
    const { counts } = await fetchSidebarData();
    expect(counts).toEqual({
      personas: 12,
      conversations: 34,
      calls: 5,
      memoryNodes: 7,
      activeTasks: 2,
      attention: 9,
      schedules: 3,
    });
  });

  it("degrades to all-zero counts when the payload is missing (fail-soft chrome)", async () => {
    api.GET.mockImplementation((path: string) =>
      Promise.resolve(path === "/v1/me/nav-counts" ? {} : respond(path)),
    );
    const { counts } = await fetchSidebarData();
    expect(counts).toEqual(EMPTY_NAV_COUNTS);
    expect(counts.attention).toBe(0); // no badge beats a wrong badge
  });

  it("degrades to all-zero counts when the whole fetch throws", async () => {
    api.GET.mockRejectedValue(new Error("cold token"));
    const data = await fetchSidebarData();
    expect(data.counts).toEqual(EMPTY_NAV_COUNTS);
    expect(data.personas).toEqual([]);
  });
});
