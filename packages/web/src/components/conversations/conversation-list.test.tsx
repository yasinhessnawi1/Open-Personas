/**
 * Spec K11 (T5, D-K11-9) — the delete-conversation dialog's opt-in "also
 * forget" checkbox. Default OFF: a plain delete leaves the persona's memory
 * intact (`forget_memory=false`); checking it threads `forget_memory=true`
 * into the DELETE. No checkbox at all when the row's persona can't be
 * resolved (nothing to label the checkbox with).
 *
 * R9-304: the dialog also says how many Memory facts the forget would remove
 * (from `GET .../forget-preview`), and a delete the API refuses (409 while
 * Memory is being reorganised) says so instead of claiming it worked.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useSearchParams } from "next/navigation";
import { NextIntlClientProvider } from "next-intl";
import { beforeEach, describe, expect, it, vi } from "vitest";
import en from "@/i18n/messages/en.json";
import {
  ConversationList,
  type ConversationListItem,
  type ConversationListPersona,
} from "./conversation-list";

const h = vi.hoisted(() => ({
  del: vi.fn(),
  // Routed by path (see beforeEach): the row menu reads the initiative-hold
  // control on open (D-ORCH-11-18-u; nothing holds these rows, so it offers no
  // item), and the delete dialog reads the forget preview (R9-304).
  get: vi.fn(),
  preview: vi.fn(),
  notify: vi.fn(),
  refresh: vi.fn(),
}));

vi.mock("@/lib/api/use-api", () => ({
  useApi: () => ({ DELETE: h.del, GET: h.get }),
}));
vi.mock("@/components/providers/notification-provider", () => ({
  useNotify: () => ({ notify: h.notify }),
}));
vi.mock("@/lib/hooks/use-sidebar-refresh", () => ({
  useSidebarRefresh: () => h.refresh,
}));
vi.mock("next/navigation", () => ({
  useSearchParams: vi.fn(),
}));

const ASTRID: ConversationListPersona = {
  id: "astrid",
  name: "Astrid Berg",
  avatar_url: null,
};

const CONVERSATIONS: ConversationListItem[] = [
  {
    id: "conv1",
    persona_id: "astrid",
    title: "Battery chemistry",
    updated_at: "2026-01-01T10:00:00Z",
  },
];

function renderList() {
  return render(
    <NextIntlClientProvider locale="en" messages={en}>
      <ConversationList
        conversations={CONVERSATIONS}
        personaById={{ astrid: ASTRID }}
      />
    </NextIntlClientProvider>,
  );
}

async function openDeleteDialog() {
  fireEvent.click(screen.getByLabelText("Actions for Battery chemistry"));
  fireEvent.click(await screen.findByRole("menuitem", { name: /Delete/ }));
}

beforeEach(() => {
  h.del.mockReset();
  h.del.mockResolvedValue({});
  h.preview.mockReset();
  h.preview.mockResolvedValue({ data: { facts_removed: 0, facts_kept: 0 } });
  h.get.mockReset();
  h.get.mockImplementation(async (path: string, init: unknown) =>
    path.endsWith("/forget-preview")
      ? h.preview(path, init)
      : {
          data: { state: "not_held", released_at: null, version: "" },
          response: new Response(null, { status: 200 }),
        },
  );
  h.notify.mockReset();
  h.refresh.mockReset();
  vi.mocked(useSearchParams).mockReturnValue(
    new URLSearchParams() as unknown as ReturnType<typeof useSearchParams>,
  );
});

describe("ConversationList — delete-conversation forget checkbox (K11-T5)", () => {
  it("shows a persona-labelled, unchecked-by-default forget checkbox", async () => {
    renderList();
    await openDeleteDialog();
    const checkbox = screen.getByRole("checkbox", {
      name: /Also forget what Astrid Berg learned here/,
    });
    expect(checkbox).not.toBeChecked();
  });

  it("default (unchecked): DELETE is sent with forget_memory=false", async () => {
    renderList();
    await openDeleteDialog();
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));

    await waitFor(() =>
      expect(h.del).toHaveBeenCalledWith(
        "/v1/conversations/{conversation_id}",
        expect.objectContaining({
          params: {
            path: { conversation_id: "conv1" },
            query: { forget_memory: false },
          },
        }),
      ),
    );
  });

  it("checking the box: DELETE is sent with forget_memory=true", async () => {
    renderList();
    await openDeleteDialog();
    fireEvent.click(
      screen.getByRole("checkbox", {
        name: /Also forget what Astrid Berg learned here/,
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));

    await waitFor(() =>
      expect(h.del).toHaveBeenCalledWith(
        "/v1/conversations/{conversation_id}",
        expect.objectContaining({
          params: {
            path: { conversation_id: "conv1" },
            query: { forget_memory: true },
          },
        }),
      ),
    );
  });

  it("cancel closes the dialog without calling DELETE", async () => {
    renderList();
    await openDeleteDialog();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(h.del).not.toHaveBeenCalled();
  });

  it("omits the checkbox when the row's persona can't be resolved", async () => {
    render(
      <NextIntlClientProvider locale="en" messages={en}>
        <ConversationList conversations={CONVERSATIONS} personaById={{}} />
      </NextIntlClientProvider>,
    );
    await openDeleteDialog();
    expect(
      screen.queryByRole("checkbox", { name: /Also forget/ }),
    ).not.toBeInTheDocument();
  });
});

describe("ConversationList: the forget's Memory fact count (R9-304)", () => {
  it("asks the preview endpoint for this conversation", async () => {
    renderList();
    await openDeleteDialog();

    await waitFor(() =>
      expect(h.get).toHaveBeenCalledWith(
        "/v1/conversations/{conversation_id}/forget-preview",
        { params: { path: { conversation_id: "conv1" } } },
      ),
    );
  });

  it("says how many facts go and how many stay", async () => {
    h.preview.mockResolvedValue({ data: { facts_removed: 3, facts_kept: 1 } });
    renderList();
    await openDeleteDialog();

    expect(
      await screen.findByText(
        "This also removes 3 facts from Memory that were learned only here.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText("1 fact also learned elsewhere stays."),
    ).toBeInTheDocument();
  });

  it("says nothing is removed when no fact comes only from here", async () => {
    h.preview.mockResolvedValue({ data: { facts_removed: 0, facts_kept: 0 } });
    renderList();
    await openDeleteDialog();

    expect(
      await screen.findByText("No facts in Memory were learned only here."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/learned elsewhere/)).not.toBeInTheDocument();
  });

  it("shows no count when the preview fails, and the delete still works", async () => {
    h.preview.mockResolvedValue({
      error: { error: "x" },
      response: { status: 500 },
    });
    renderList();
    await openDeleteDialog();
    await waitFor(() => expect(h.preview).toHaveBeenCalled());

    expect(screen.queryByText(/from Memory/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(h.del).toHaveBeenCalled());
  });

  it("a refused delete says it failed, not that it worked", async () => {
    h.del.mockResolvedValue({
      error: { error: "memory_node_changed" },
      response: { status: 409 },
    });
    renderList();
    await openDeleteDialog();
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));

    await waitFor(() =>
      expect(h.notify).toHaveBeenCalledWith(
        expect.objectContaining({
          level: "error",
          title: "Couldn't delete Battery chemistry. Try again in a moment.",
        }),
      ),
    );
    expect(h.refresh).not.toHaveBeenCalled();
  });

  it("a conversation already gone (another tab) refreshes the list instead", async () => {
    h.del.mockResolvedValue({
      error: { error: "conversation_not_found" },
      response: { status: 404 },
    });
    renderList();
    await openDeleteDialog();
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));

    await waitFor(() => expect(h.refresh).toHaveBeenCalled());
    expect(h.notify).not.toHaveBeenCalledWith(
      expect.objectContaining({ level: "error" }),
    );
  });
});
