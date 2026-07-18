/**
 * Spec K11 (T5, D-K11-1 / D-K11-4) — `<MemoryDetailPanel>` delete-flow tests.
 *
 * A concept-node delete now previews cross-persona episodic evidence first
 * (`forget-preview`): candidates open the cross-layer `<ForgetConfirmDialog>`
 * (never the old plain confirm); no candidates — or a preview failure — falls
 * back to the pre-K11 plain-delete confirm + `DELETE /nodes/{id}` (D-K11-4
 * back-compat). Reuses the real `<ForgetConfirmDialog>` (not mocked) so the
 * body it POSTs is proven end-to-end, not just the wiring intent.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { beforeEach, describe, expect, it, vi } from "vitest";
import en from "@/i18n/messages/en.json";
import type { ForgetCandidate, MemoryNodeDetail } from "@/lib/api";
import { MemoryDetailPanel } from "./memory-detail-panel";

const h = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  del: vi.fn(),
  patch: vi.fn(),
  confirm: vi.fn(async () => true),
  notify: vi.fn(),
  push: vi.fn(),
}));

vi.mock("@/lib/api/use-api", () => ({
  useApi: () => ({
    GET: h.get,
    POST: h.post,
    DELETE: h.del,
    PATCH: h.patch,
  }),
}));
vi.mock("@/components/providers/confirm-provider", () => ({
  useConfirm: () => h.confirm,
}));
vi.mock("@/components/providers/notification-provider", () => ({
  useNotify: () => ({ notify: h.notify }),
}));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: h.push }),
}));

const DETAIL: MemoryNodeDetail = {
  id: "n1",
  kind: "fact",
  label: "Balto is a dog",
  content: "Balto is the user's dog.",
  wellbeing_category: null,
  created_at: "2026-01-01T10:00:00Z",
  origin: {
    source: "persona_self",
    persona_id: "astrid",
    persona_name: "Astrid Berg",
    interaction_id: null,
    conversation_id: null,
    written_at: "2026-01-01T10:00:00Z",
  },
  evolution: [],
  links: [],
};

const CANDIDATES: ForgetCandidate[] = [
  {
    persona_id: "astrid",
    persona_name: "Astrid Berg",
    chunk_id: "c1",
    kind: "raw",
    text: "USER: Balto is my dog.",
    score: 0.91,
  },
];

function renderPanel() {
  const onDeleted = vi.fn();
  render(
    <NextIntlClientProvider locale="en" messages={en}>
      <MemoryDetailPanel
        nodeId="n1"
        onClose={vi.fn()}
        onTraverse={vi.fn()}
        onDeleted={onDeleted}
        initialDetail={DETAIL}
      />
    </NextIntlClientProvider>,
  );
  return { onDeleted };
}

beforeEach(() => {
  h.get.mockReset();
  h.post.mockReset();
  h.del.mockReset();
  h.patch.mockReset();
  h.confirm.mockReset();
  h.confirm.mockResolvedValue(true);
  h.notify.mockReset();
  h.push.mockReset();
  h.del.mockResolvedValue({ data: {} });
});

describe("MemoryDetailPanel — cross-layer forget (K11-T5)", () => {
  it("no episodic candidates: previews, then falls back to the plain confirm + DELETE", async () => {
    h.post.mockResolvedValueOnce({ data: { candidates: [] } });
    const { onDeleted } = renderPanel();

    fireEvent.click(screen.getByRole("button", { name: /Delete/ }));

    await waitFor(() =>
      expect(h.post).toHaveBeenCalledWith(
        "/v1/memory/nodes/{node_id}/forget-preview",
        expect.objectContaining({ params: { path: { node_id: "n1" } } }),
      ),
    );
    await waitFor(() => expect(h.confirm).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(h.del).toHaveBeenCalledWith(
        "/v1/memory/nodes/{node_id}",
        expect.objectContaining({ params: { path: { node_id: "n1" } } }),
      ),
    );
    await waitFor(() => expect(onDeleted).toHaveBeenCalledWith("n1"));
    // The candidate dialog never appeared for an empty preview.
    expect(screen.queryByText(/Also forget/)).not.toBeInTheDocument();
  });

  it("declining the plain confirm leaves the node intact", async () => {
    h.post.mockResolvedValueOnce({ data: { candidates: [] } });
    h.confirm.mockResolvedValueOnce(false);
    const { onDeleted } = renderPanel();

    fireEvent.click(screen.getByRole("button", { name: /Delete/ }));
    await waitFor(() => expect(h.confirm).toHaveBeenCalledTimes(1));
    expect(h.del).not.toHaveBeenCalled();
    expect(onDeleted).not.toHaveBeenCalled();
  });

  it("candidates found: opens the cross-layer confirm instead of the plain confirm", async () => {
    h.post.mockResolvedValueOnce({ data: { candidates: CANDIDATES } });
    renderPanel();

    fireEvent.click(screen.getByRole("button", { name: /Delete/ }));

    expect(await screen.findByText(/Also forget/)).toBeInTheDocument();
    expect(screen.getByText("Astrid Berg")).toBeInTheDocument();
    // The old plain confirm never fires when there's evidence to review.
    expect(h.confirm).not.toHaveBeenCalled();
    expect(h.del).not.toHaveBeenCalled();
  });

  it("confirming the cross-layer dialog POSTs the kept set and forgets everywhere", async () => {
    h.post.mockResolvedValueOnce({ data: { candidates: CANDIDATES } });
    h.post.mockResolvedValueOnce({ data: {} }); // the forget commit
    const { onDeleted } = renderPanel();

    fireEvent.click(screen.getByRole("button", { name: /Delete/ }));
    await screen.findByText(/Also forget/);
    fireEvent.click(screen.getByRole("button", { name: "Forget everywhere" }));

    await waitFor(() =>
      expect(h.post).toHaveBeenCalledWith(
        "/v1/memory/nodes/{node_id}/forget",
        expect.objectContaining({
          params: { path: { node_id: "n1" } },
          body: { episodic: [{ persona_id: "astrid", chunk_id: "c1" }] },
        }),
      ),
    );
    await waitFor(() => expect(onDeleted).toHaveBeenCalledWith("n1"));
    // The plain DELETE never fires on the cross-layer path.
    expect(h.del).not.toHaveBeenCalled();
  });

  it("cancelling the cross-layer dialog forgets nothing", async () => {
    h.post.mockResolvedValueOnce({ data: { candidates: CANDIDATES } });
    const { onDeleted } = renderPanel();

    fireEvent.click(screen.getByRole("button", { name: /Delete/ }));
    await screen.findByText(/Also forget/);
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.queryByText(/Also forget/)).not.toBeInTheDocument();
    expect(h.del).not.toHaveBeenCalled();
    expect(onDeleted).not.toHaveBeenCalled();
    expect(h.post).toHaveBeenCalledTimes(1); // preview only
  });

  it("a preview failure falls back to the plain confirm + DELETE rather than blocking", async () => {
    h.post.mockRejectedValueOnce(new Error("network"));
    const { onDeleted } = renderPanel();

    fireEvent.click(screen.getByRole("button", { name: /Delete/ }));
    await waitFor(() => expect(h.confirm).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(h.del).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(onDeleted).toHaveBeenCalledWith("n1"));
  });
});
