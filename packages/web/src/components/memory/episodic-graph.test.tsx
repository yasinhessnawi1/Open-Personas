/**
 * Spec K11 (T4, D-K11-5) — `<EpisodicGraph>` component tests.
 *
 * `<MemoryCanvas>` is a Web-Worker-driven `<canvas>` renderer (graphology +
 * ForceAtlas2) that jsdom can't host (no `ResizeObserver`, no 2-D context) —
 * no existing K5 memory component is unit-tested for that reason, so this
 * mocks the dynamically-imported canvas with a thin stub that exposes exactly
 * what `<EpisodicGraph>` hands it (`nodes` + `onSelectNode`) and asserts the
 * REAL contract: persona-scoped fetch, gist→node/edge adaptation, expand-on-
 * demand drill-down, and the forget action's confirm→DELETE→refetch sequence
 * (D-K11-6's cascade — a forget always re-fetches rather than reconciling
 * locally).
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AvatarPersona } from "@/components/persona/persona-avatar";
import { EpisodicGraph } from "./episodic-graph";

const h = vi.hoisted(() => ({
  get: vi.fn(),
  del: vi.fn(async () => ({}) as { data?: undefined; error?: undefined }),
  confirm: vi.fn(async () => true),
  notify: vi.fn(),
}));

vi.mock("@/lib/api/use-api", () => ({
  useApi: () => ({
    GET: h.get,
    DELETE: h.del,
    POST: vi.fn(),
    PATCH: vi.fn(),
  }),
}));
vi.mock("@/components/providers/confirm-provider", () => ({
  useConfirm: () => h.confirm,
}));
vi.mock("@/components/providers/notification-provider", () => ({
  useNotify: () => ({ notify: h.notify }),
}));
// The dynamically-imported canvas — stubbed to a plain node list so a click
// can drive `onSelectNode` without a real `<canvas>` / Web Worker.
vi.mock("./memory-canvas", () => ({
  MemoryCanvas: ({
    nodes,
    onSelectNode,
  }: {
    nodes: readonly { id: string; label: string }[];
    onSelectNode: (id: string | null) => void;
  }) => (
    <div data-testid="canvas">
      {nodes.map((n) => (
        <button key={n.id} type="button" onClick={() => onSelectNode(n.id)}>
          {n.label}
        </button>
      ))}
    </div>
  ),
}));

const messages = {
  personaPicker: {
    choosePersona: "Choose a persona",
    empty: "No personas yet. Create one to start a chat.",
  },
  memory: {
    loading: "Drawing your map…",
    loadError: "Couldn't open this memory. Please try again.",
    close: "Close",
    episodicPersonaLabel: "Choose whose episodic memory to browse",
    episodicPersonaPlaceholder: "Select a persona",
    episodicGistsWord: "memories",
    episodicNoPersona: "Choose a persona",
    episodicNoPersonaHint:
      "Episodic memory is per-persona, so pick one above to browse what they've recalled.",
    episodicUnavailable: "Episodic memory isn't available here",
    episodicUnavailableHint:
      "This deployment doesn't run an episodic memory backend, so there's nothing to browse yet.",
    episodicEmpty: "Nothing recalled yet",
    episodicEmptyHint:
      "As this persona talks with you, what it recalls turn-by-turn grows into a map here.",
    episodicDetailLabel: "Episodic memory detail",
    episodicKindGist: "Compressed memory",
    episodicKindMember: "Raw memory",
    episodicMemberCount:
      "{count, plural, =0 {no raw memories} one {# raw memory} other {# raw memories}}",
    episodicExpand: "Show raw memories",
    episodicExpanding: "Loading…",
    episodicExpandError: "Couldn't load the raw memories. Please try again.",
    episodicCollapse: "Hide raw memories",
    episodicForget: "Forget",
    episodicForgotten: "Forgotten",
    episodicForgetError: "Couldn't forget this memory. Please try again.",
    episodicForgetGistTitle: "Forget this compressed memory?",
    episodicForgetGistBody:
      "This removes every raw memory it summarises. This can't be undone.",
    episodicForgetMemberTitle: "Forget this raw memory?",
    episodicForgetMemberBody:
      "This also removes the compressed memory it belongs to (it can't summarise nothing). This can't be undone.",
    episodicForgetConfirm: "Forget",
  },
};

const PERSONAS: AvatarPersona[] = [
  { id: "astrid", name: "Astrid Berg", avatar_url: null },
  { id: "lena", name: "Lena Brevik", avatar_url: null },
];

const GIST = {
  id: "g1",
  text: "Talked about grid batteries",
  member_ids: ["m1", "m2"],
  created_at: "2026-01-01T10:00:00Z",
};

const MEMBERS = [
  {
    id: "m1",
    text: "USER: What battery chemistry is best?",
    created_at: "2026-01-01T09:59:00Z",
  },
  {
    id: "m2",
    text: "ASSISTANT: LFP is a safe default.",
    created_at: "2026-01-01T09:59:30Z",
  },
];

function renderGraph(personas: readonly AvatarPersona[] = PERSONAS) {
  return render(
    <NextIntlClientProvider locale="en" messages={messages}>
      <EpisodicGraph personas={personas} />
    </NextIntlClientProvider>,
  );
}

describe("EpisodicGraph", () => {
  beforeEach(() => {
    h.get.mockReset();
    h.del.mockReset();
    h.del.mockResolvedValue({});
    h.confirm.mockReset();
    h.confirm.mockResolvedValue(true);
    h.notify.mockReset();
    h.get.mockImplementation(async (path: string) => {
      if (path === "/v1/memory/episodic") {
        return { data: { available: true, gists: [GIST] } };
      }
      if (path === "/v1/memory/episodic/{gist_id}/members") {
        return { data: { members: MEMBERS } };
      }
      return { data: {} };
    });
  });

  it("fetches the first persona's episodic window on mount and renders its gists", async () => {
    renderGraph();
    await waitFor(() =>
      expect(h.get).toHaveBeenCalledWith(
        "/v1/memory/episodic",
        expect.objectContaining({
          params: { query: { persona_id: "astrid" } },
        }),
      ),
    );
    expect(await screen.findByText(GIST.text)).toBeInTheDocument();
  });

  it("switching persona via the picker refetches that persona's window", async () => {
    renderGraph();
    await waitFor(() => expect(h.get).toHaveBeenCalledTimes(1));

    fireEvent.click(
      screen.getByLabelText("Choose whose episodic memory to browse"),
    );
    await waitFor(() => {
      expect(
        screen.getByRole("menuitem", { name: /Lena Brevik/ }),
      ).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole("menuitem", { name: /Lena Brevik/ }));

    await waitFor(() =>
      expect(h.get).toHaveBeenCalledWith(
        "/v1/memory/episodic",
        expect.objectContaining({
          params: { query: { persona_id: "lena" } },
        }),
      ),
    );
  });

  it("selecting a gist opens the detail panel; expanding loads its raw members as new nodes", async () => {
    renderGraph();
    const gistNode = await screen.findByText(GIST.text);
    fireEvent.click(gistNode);

    expect(await screen.findByText("Compressed memory")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Show raw memories" }));

    await waitFor(() =>
      expect(h.get).toHaveBeenCalledWith(
        "/v1/memory/episodic/{gist_id}/members",
        expect.objectContaining({
          params: {
            path: { gist_id: "g1" },
            query: { persona_id: "astrid" },
          },
        }),
      ),
    );
    expect(await screen.findByText(MEMBERS[0].text)).toBeInTheDocument();
    expect(await screen.findByText(MEMBERS[1].text)).toBeInTheDocument();
  });

  it("forgetting a gist confirms, calls DELETE with is_gist=true, and refetches the window", async () => {
    renderGraph();
    const gistNode = await screen.findByText(GIST.text);
    fireEvent.click(gistNode);

    fireEvent.click(screen.getByRole("button", { name: /Forget/ }));

    await waitFor(() => expect(h.confirm).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(h.del).toHaveBeenCalledWith(
        "/v1/memory/episodic/{chunk_id}",
        expect.objectContaining({
          params: {
            path: { chunk_id: "g1" },
            query: { persona_id: "astrid", is_gist: true },
          },
        }),
      ),
    );
    // The cascade (D-K11-6) means the client re-fetches rather than
    // reconciling locally — the initial load + the post-forget refetch.
    await waitFor(() => expect(h.get).toHaveBeenCalledTimes(2));
  });

  it("does not call DELETE when the forget confirm is declined", async () => {
    h.confirm.mockResolvedValueOnce(false);
    renderGraph();
    const gistNode = await screen.findByText(GIST.text);
    fireEvent.click(gistNode);
    fireEvent.click(screen.getByRole("button", { name: /Forget/ }));
    await waitFor(() => expect(h.confirm).toHaveBeenCalledTimes(1));
    expect(h.del).not.toHaveBeenCalled();
  });

  it("shows a choose-a-persona empty state when there are no personas", () => {
    renderGraph([]);
    expect(
      screen.getByRole("heading", { name: "Choose a persona" }),
    ).toBeInTheDocument();
    expect(h.get).not.toHaveBeenCalled();
  });

  it("shows the unavailable empty state when the episodic backend isn't composed", async () => {
    h.get.mockImplementation(async () => ({
      data: { available: false, gists: [] },
    }));
    renderGraph();
    expect(
      await screen.findByRole("heading", {
        name: "Episodic memory isn't available here",
      }),
    ).toBeInTheDocument();
  });

  it("shows the empty state when the persona has no episodic memories yet", async () => {
    h.get.mockImplementation(async () => ({
      data: { available: true, gists: [] },
    }));
    renderGraph();
    expect(
      await screen.findByRole("heading", { name: "Nothing recalled yet" }),
    ).toBeInTheDocument();
  });
});
