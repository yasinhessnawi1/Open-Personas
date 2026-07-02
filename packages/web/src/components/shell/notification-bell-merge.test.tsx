/**
 * Spec P6 (D4-e) — the bell renders the UNION (D-P6-7) of the client
 * `useNotify()` feed and the durable `useServerNotifications()` feed, newest-first.
 */

import { act, fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/components/patterns/toast", () => ({
  toast: { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() },
}));
vi.mock("@/auth", () => ({
  useAuth: () => ({ getToken: vi.fn().mockResolvedValue("jwt-token") }),
}));
vi.mock("next/navigation", () => ({ usePathname: () => "/" }));

import {
  NotificationProvider,
  useNotify,
} from "@/components/providers/notification-provider";
import { ServerNotificationsProvider } from "@/components/providers/server-notifications-provider";
import { NotificationBell } from "./notification-bell";

const messages = {
  notifications: {
    open: "Notifications",
    title: "Notifications",
    empty: "Nothing yet.",
    clear: "Clear all",
    unreadLabel: "{count} unread",
    personaFallback: "your persona",
    run: { completed: "Task finished · {persona}" },
    persona: { ready: "{persona} is ready" },
  },
};

const serverRow = {
  id: "s1",
  kind: "run_terminal",
  ref_id: "abc",
  level: "success",
  message_key: "notifications.run.completed",
  params: { persona: "Ada" },
  read: false,
  created_at: "2026-06-25T12:00:00Z",
};

const fetchMock = vi.fn();

function ClientSeeder() {
  const { notify } = useNotify();
  return (
    <button
      type="button"
      onClick={() =>
        notify({
          level: "success",
          title: "Persona duplicated",
          href: "/personas/x",
        })
      }
    >
      seed
    </button>
  );
}

function renderBell(children?: ReactNode) {
  return render(
    <NextIntlClientProvider locale="en" messages={messages}>
      <NotificationProvider>
        <ServerNotificationsProvider>
          <NotificationBell />
          {children}
        </ServerNotificationsProvider>
      </NotificationProvider>
    </NextIntlClientProvider>,
  );
}

beforeEach(() => {
  window.localStorage.clear();
  vi.clearAllMocks();
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
    if (
      String(url).endsWith("/v1/me/notifications") &&
      init?.method !== "POST"
    ) {
      return { ok: true, json: async () => [serverRow] };
    }
    return { ok: true, json: async () => ({ updated: 1 }) };
  });
});

describe("NotificationBell — client ∪ server merge", () => {
  it("shows both a client entry and a server entry, each deep-linked", async () => {
    renderBell(<ClientSeeder />);
    // Seed a client entry, then open the bell (entries render only while open).
    act(() => {
      fireEvent.click(screen.getByText("seed"));
    });
    fireEvent.click(screen.getByRole("button", { name: "Notifications" }));

    // Client entry is there immediately; the server entry appears once the poll
    // lands and the open panel re-renders — both deep-linked.
    expect(
      screen.getByRole("link", { name: /Persona duplicated/ }),
    ).toHaveAttribute("href", "/personas/x");
    const serverLink = await screen.findByRole("link", {
      name: /Task finished · Ada/,
    });
    expect(serverLink).toHaveAttribute("href", "/runs/abc");
  });
});
