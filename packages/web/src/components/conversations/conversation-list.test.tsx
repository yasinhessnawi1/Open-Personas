/**
 * Spec K11 (T5, D-K11-9) — the delete-conversation dialog's opt-in "also
 * forget" checkbox. Default OFF: a plain delete leaves the persona's memory
 * intact (`forget_memory=false`); checking it threads `forget_memory=true`
 * into the DELETE. No checkbox at all when the row's persona can't be
 * resolved (nothing to label the checkbox with).
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
  notify: vi.fn(),
  refresh: vi.fn(),
}));

vi.mock("@/lib/api/use-api", () => ({
  useApi: () => ({ DELETE: h.del }),
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
