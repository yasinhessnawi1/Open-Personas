/**
 * D-ORCH-11-18-u: the per-conversation "fine to bring this up" control.
 *
 * The row menu reads the control when it opens. A held conversation offers a
 * release, which only goes out after the confirm dialog explains it; the
 * release carries the version the menu read. A released conversation offers
 * the undo, which goes out at once (holding back is the safe direction). A
 * conversation nothing holds shows no item. A version that moved (409) is
 * reported, not retried.
 */
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { useSearchParams } from "next/navigation";
import { NextIntlClientProvider } from "next-intl";
import { beforeEach, describe, expect, it, vi } from "vitest";
import en from "@/i18n/messages/en.json";
import { ConversationList } from "./conversation-list";

const h = vi.hoisted(() => ({
  get: vi.fn(),
  put: vi.fn(),
  del: vi.fn(),
  notify: vi.fn(),
  refresh: vi.fn(),
}));

vi.mock("@/lib/api/use-api", () => ({
  useApi: () => ({ GET: h.get, PUT: h.put, DELETE: h.del }),
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

const PATH = "/v1/conversations/{conversation_id}/initiative-hold";

function ok(data: unknown) {
  return { data, response: new Response(null, { status: 200 }) };
}

function hold(state: "held" | "released" | "not_held", version = 2) {
  return ok({
    state,
    released_at: state === "released" ? "2026-11-18T12:00:00Z" : null,
    version,
  });
}

function renderList() {
  return render(
    <NextIntlClientProvider locale="en" messages={en}>
      <ConversationList
        conversations={[
          {
            id: "conv1",
            persona_id: "astrid",
            title: "Dinner plans",
            updated_at: "2026-01-01T10:00:00Z",
          },
        ]}
        personaById={{
          astrid: { id: "astrid", name: "Astrid Berg", avatar_url: null },
        }}
      />
    </NextIntlClientProvider>,
  );
}

function openMenu() {
  fireEvent.click(screen.getByLabelText("Actions for Dinner plans"));
}

beforeEach(() => {
  for (const fn of [h.get, h.put, h.del, h.notify, h.refresh]) fn.mockReset();
  h.put.mockResolvedValue(hold("released"));
  vi.mocked(useSearchParams).mockReturnValue(
    new URLSearchParams() as unknown as ReturnType<typeof useSearchParams>,
  );
});

describe("ConversationList: the fine to bring this up control", () => {
  it("reads the control for the row when its menu opens", async () => {
    h.get.mockResolvedValue(hold("held"));
    renderList();
    expect(h.get).not.toHaveBeenCalled();
    openMenu();
    await screen.findByRole("menuitem", {
      name: "Let Astrid Berg bring this up again",
    });
    expect(h.get).toHaveBeenCalledWith(PATH, {
      params: { path: { conversation_id: "conv1" } },
    });
  });

  it("shows no item for a conversation nothing holds", async () => {
    h.get.mockResolvedValue(hold("not_held", 0));
    renderList();
    openMenu();
    await screen.findByRole("menuitem", { name: /Delete/ });
    await waitFor(() => expect(h.get).toHaveBeenCalled());
    expect(screen.queryByRole("menuitem", { name: /bring this up/ })).toBe(
      null,
    );
    expect(screen.queryByRole("menuitem", { name: /Keep this out/ })).toBe(
      null,
    );
  });

  it("releases only after the confirm explains it, for the version read", async () => {
    h.get.mockResolvedValue(hold("held", 3));
    renderList();
    openMenu();
    fireEvent.click(
      await screen.findByRole("menuitem", {
        name: "Let Astrid Berg bring this up again",
      }),
    );
    const dialog = await screen.findByRole("dialog");
    expect(
      within(dialog).getByText("Let Astrid Berg bring this conversation up?"),
    ).toBeTruthy();
    const body = dialog.textContent ?? "";
    expect(body).toContain("never brings up what it marked as sensitive");
    expect(body).not.toContain("sensitive part");
    expect(body).toContain("held back again");
    expect(body).toContain("undo this");
    expect(body.toLowerCase()).not.toContain("task");
    expect(h.put).not.toHaveBeenCalled();

    fireEvent.click(within(dialog).getByRole("button", { name: "Allow" }));
    await waitFor(() =>
      expect(h.put).toHaveBeenCalledWith(PATH, {
        params: { path: { conversation_id: "conv1" } },
        body: { released: true, version: 3 },
      }),
    );
    await waitFor(() =>
      expect(h.notify).toHaveBeenCalledWith(
        expect.objectContaining({ level: "success" }),
      ),
    );
  });

  it("says so when the release comes back still held", async () => {
    // PR #44 review MEDIUM-1: success is the answer, never the attempt.
    h.get.mockResolvedValue(hold("held"));
    h.put.mockResolvedValue(hold("held"));
    renderList();
    openMenu();
    fireEvent.click(
      await screen.findByRole("menuitem", { name: /bring this up again/ }),
    );
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Allow" }));
    await waitFor(() =>
      expect(h.notify).toHaveBeenCalledWith(
        expect.objectContaining({
          level: "warning",
          title: en.conversations.stillHeld,
        }),
      ),
    );
    expect(h.notify).not.toHaveBeenCalledWith(
      expect.objectContaining({ level: "success" }),
    );
  });

  it("says so when an undo comes back still released", async () => {
    h.get.mockResolvedValue(hold("released"));
    h.put.mockResolvedValue(hold("released"));
    renderList();
    openMenu();
    fireEvent.click(
      await screen.findByRole("menuitem", { name: /Keep this out/ }),
    );
    await waitFor(() =>
      expect(h.notify).toHaveBeenCalledWith(
        expect.objectContaining({
          level: "error",
          title: en.conversations.holdFailed,
        }),
      ),
    );
  });

  it("writes nothing when the confirm is cancelled", async () => {
    h.get.mockResolvedValue(hold("held"));
    renderList();
    openMenu();
    fireEvent.click(
      await screen.findByRole("menuitem", { name: /bring this up again/ }),
    );
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBe(null));
    expect(h.put).not.toHaveBeenCalled();
  });

  it("holds a released conversation back again at once", async () => {
    h.get.mockResolvedValue(hold("released"));
    h.put.mockResolvedValue(hold("held"));
    renderList();
    openMenu();
    fireEvent.click(
      await screen.findByRole("menuitem", {
        name: "Keep this out of Astrid Berg's suggestions",
      }),
    );
    await waitFor(() =>
      expect(h.put).toHaveBeenCalledWith(PATH, {
        params: { path: { conversation_id: "conv1" } },
        body: { released: false },
      }),
    );
    expect(screen.queryByRole("dialog")).toBe(null);
  });

  it("reports a conversation that changed since the menu read it", async () => {
    h.get.mockResolvedValue(hold("held"));
    h.put.mockResolvedValue({
      error: { error: "conversation_changed" },
      response: new Response(null, { status: 409 }),
    });
    renderList();
    openMenu();
    fireEvent.click(
      await screen.findByRole("menuitem", { name: /bring this up again/ }),
    );
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Allow" }));
    await waitFor(() =>
      expect(h.notify).toHaveBeenCalledWith(
        expect.objectContaining({
          level: "warning",
          title: en.conversations.holdChanged,
        }),
      ),
    );
    expect(h.put).toHaveBeenCalledTimes(1);
  });
});
