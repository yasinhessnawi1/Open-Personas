/**
 * R9-038 — usePersonaWorkingIds: the sidebar rail's per-persona "working" signal.
 *
 * Unions two REUSED, already-live client sources — never a new backend
 * endpoint (see the hook's own docstring for the investigation trail):
 *   - useActiveWork().activeChats (Spec P1 D-P1-v7-indicator): persona-tagged
 *     detached chat turns, independent of the current route.
 *   - GET /v1/tasks (Spec A6 fetchTasks) filtered to status === "progressing"
 *     — a task genuinely mid-flight, not merely non-terminal (just_created /
 *     waiting_on_user / scheduled / paused are parked, not "live").
 */
import { renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { emitTaskSignal } from "@/lib/task-signal";
import { usePersonaWorkingIds } from "./use-persona-working-ids";

// A STABLE getToken (the task-detail.test.tsx precedent) — a fresh identity
// each render would re-fire the mount effect and inflate the fetch count.
const auth = vi.hoisted(() => ({
  getToken: () => Promise.resolve("test-token"),
}));

const tasksClient = vi.hoisted(() => ({
  fetchTasks: vi.fn(
    async () => [] as Array<{ persona_id: string; status: string }>,
  ),
}));

const activeWork = vi.hoisted(() => ({
  activeChats: [] as Array<{ conversationId: string; personaId: string }>,
}));

vi.mock("@/auth", () => ({
  useAuth: () => ({ getToken: auth.getToken }),
}));
vi.mock("@/lib/api/tasks-client", () => tasksClient);
vi.mock("@/lib/work/active-work-context", () => ({
  useActiveWork: () => ({ activeChats: activeWork.activeChats }),
}));

beforeEach(() => {
  tasksClient.fetchTasks.mockReset();
  tasksClient.fetchTasks.mockResolvedValue([]);
  activeWork.activeChats = [];
});

describe("usePersonaWorkingIds", () => {
  it("includes persona ids for 'progressing' tasks after the mount fetch resolves", async () => {
    tasksClient.fetchTasks.mockResolvedValue([
      { persona_id: "astrid", status: "progressing" },
      { persona_id: "kai", status: "waiting_on_user" },
    ]);
    const { result } = renderHook(() => usePersonaWorkingIds());
    await waitFor(() => expect(result.current.has("astrid")).toBe(true));
    // waiting_on_user is non-terminal but parked (blocked on the owner) —
    // NOT "live right now", so it must not light the working ring.
    expect(result.current.has("kai")).toBe(false);
  });

  it.each([
    ["just_created"],
    ["waiting_on_user"],
    ["scheduled"],
    ["paused"],
    ["completed"],
    ["failed"],
    ["cancelled"],
  ])(
    "excludes the non-'progressing' status %s from the working set",
    async (status) => {
      tasksClient.fetchTasks.mockResolvedValue([
        { persona_id: "astrid", status },
      ]);
      const { result } = renderHook(() => usePersonaWorkingIds());
      await waitFor(() => expect(tasksClient.fetchTasks).toHaveBeenCalled());
      expect(result.current.has("astrid")).toBe(false);
    },
  );

  it("unions activeChats' persona ids with task-derived persona ids (a persona in both appears once)", async () => {
    activeWork.activeChats = [{ conversationId: "c1", personaId: "astrid" }];
    tasksClient.fetchTasks.mockResolvedValue([
      { persona_id: "astrid", status: "progressing" },
      { persona_id: "kai", status: "progressing" },
    ]);
    const { result } = renderHook(() => usePersonaWorkingIds());
    await waitFor(() => expect(result.current.size).toBe(2));
    expect([...result.current].sort()).toEqual(["astrid", "kai"]);
  });

  it("activeChats alone (no progressing tasks) still contributes its persona ids", async () => {
    activeWork.activeChats = [{ conversationId: "c1", personaId: "mira" }];
    const { result } = renderHook(() => usePersonaWorkingIds());
    await waitFor(() => expect(result.current.has("mira")).toBe(true));
  });

  it("fails soft on a fetch error — never throws, activeChats still contributes", async () => {
    activeWork.activeChats = [{ conversationId: "c1", personaId: "mira" }];
    tasksClient.fetchTasks.mockRejectedValue(new Error("network down"));
    const { result } = renderHook(() => usePersonaWorkingIds());
    await waitFor(() => expect(result.current.has("mira")).toBe(true));
  });

  it("refetches on a task.updated signal (W8 refetch-on-ping, never trusting the pushed payload)", async () => {
    const { result } = renderHook(() => usePersonaWorkingIds());
    await waitFor(() =>
      expect(tasksClient.fetchTasks).toHaveBeenCalledTimes(1),
    );
    expect(result.current.has("astrid")).toBe(false);

    tasksClient.fetchTasks.mockResolvedValue([
      { persona_id: "astrid", status: "progressing" },
    ]);
    emitTaskSignal({ id: "r9-038-test:1", taskId: "t1" });

    await waitFor(() => expect(result.current.has("astrid")).toBe(true));
    expect(tasksClient.fetchTasks).toHaveBeenCalledTimes(2);
  });
});
