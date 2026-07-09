/**
 * Spec P6 (D4-e) — the durable, cross-device server notification feed provider.
 *
 * Proven against the REAL endpoint shape (fetch is mocked at the transport
 * boundary): rows map to deep-linked entries; the first poll is a silent baseline;
 * a newly-appearing unread row toasts UNLESS its run is the current route
 * (no-double-signal, D-P6-9); mark-read hits the API + is optimistic.
 */

import { act, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { beforeEach, describe, expect, it, vi } from "vitest";

const toastFns = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
  info: vi.fn(),
  warning: vi.fn(),
}));
vi.mock("@/components/patterns/toast", () => ({ toast: toastFns }));
vi.mock("@/auth", () => ({
  useAuth: () => ({ getToken: vi.fn().mockResolvedValue("jwt-token") }),
}));
let pathname = "/";
vi.mock("next/navigation", () => ({ usePathname: () => pathname }));

import {
  ServerNotificationsProvider,
  useServerNotifications,
} from "./server-notifications-provider";

const messages = {
  notifications: {
    personaFallback: "your persona",
    genericUpdate: "{persona} has an update",
    run: {
      completed: "Task finished · {persona}",
      failed: "Task failed · {persona}",
    },
    persona: { ready: "{persona} is ready" },
    schedule: { fired: "{persona} ran your reminder: {subject}" },
  },
};

interface Row {
  id: string;
  kind: string;
  ref_id: string | null;
  level: string;
  message_key: string;
  params: Record<string, string>;
  read: boolean;
  created_at: string;
}

function row(
  id: string,
  kind: string,
  ref: string | null,
  extra: Partial<Row> = {},
): Row {
  return {
    id,
    kind,
    ref_id: ref,
    level: "success",
    message_key:
      kind === "run_terminal"
        ? "notifications.run.completed"
        : "notifications.persona.ready",
    params: { persona: "Ada" },
    read: false,
    created_at: "2026-06-25T12:00:00Z",
    ...extra,
  };
}

let currentRows: Row[] = [];
const fetchMock = vi.fn();

function Probe() {
  const { entries, unreadCount, markAllRead, markRead } =
    useServerNotifications();
  return (
    <div>
      <div
        data-testid="summary"
        data-count={entries.length}
        data-unread={unreadCount}
      />
      {entries.map((e) => (
        <div
          key={e.id}
          data-testid={`entry-${e.id}`}
          data-href={e.href ?? ""}
          data-title={e.title}
          data-read={String(e.read)}
        />
      ))}
      <button type="button" onClick={markAllRead}>
        all
      </button>
      <button type="button" onClick={() => markRead("n1")}>
        one
      </button>
    </div>
  );
}

function renderProvider() {
  return render(
    <NextIntlClientProvider locale="en" messages={messages}>
      <ServerNotificationsProvider>
        <Probe />
      </ServerNotificationsProvider>
    </NextIntlClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  pathname = "/";
  currentRows = [];
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
    const isPost = init?.method === "POST";
    if (String(url).endsWith("/v1/me/notifications") && !isPost) {
      return { ok: true, json: async () => currentRows };
    }
    return { ok: true, json: async () => ({ updated: 1 }) };
  });
});

describe("ServerNotificationsProvider", () => {
  it("maps rows to deep-linked entries (run → /runs, persona → /personas)", async () => {
    currentRows = [
      row("n1", "run_terminal", "abc"),
      row("n2", "persona_ready", "p9"),
    ];
    renderProvider();
    await waitFor(() =>
      expect(screen.getByTestId("summary")).toHaveAttribute("data-count", "2"),
    );
    expect(screen.getByTestId("entry-n1")).toHaveAttribute(
      "data-href",
      "/runs/abc",
    );
    expect(screen.getByTestId("entry-n1")).toHaveAttribute(
      "data-title",
      "Task finished · Ada",
    );
    expect(screen.getByTestId("entry-n2")).toHaveAttribute(
      "data-href",
      "/personas/p9",
    );
  });

  it("does not toast on the first poll (silent baseline)", async () => {
    currentRows = [row("n1", "run_terminal", "abc")];
    renderProvider();
    await waitFor(() =>
      expect(screen.getByTestId("summary")).toHaveAttribute("data-count", "1"),
    );
    expect(toastFns.success).not.toHaveBeenCalled();
  });

  it("toasts a newly-appearing unread notification on a later poll", async () => {
    currentRows = [row("n1", "run_terminal", "abc")];
    renderProvider();
    await waitFor(() =>
      expect(screen.getByTestId("summary")).toHaveAttribute("data-count", "1"),
    );
    // A new persona-ready lands; a window focus re-polls.
    currentRows = [
      row("n2", "persona_ready", "p9"),
      row("n1", "run_terminal", "abc"),
    ];
    act(() => {
      window.dispatchEvent(new Event("focus"));
    });
    await waitFor(() =>
      expect(toastFns.success).toHaveBeenCalledWith("Ada is ready"),
    );
  });

  it("suppresses the toast for a run-terminal whose run is the current route", async () => {
    currentRows = [];
    pathname = "/runs/live";
    renderProvider();
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    // The run the user is viewing finishes; a re-poll surfaces it.
    currentRows = [row("nlive", "run_terminal", "live")];
    act(() => {
      window.dispatchEvent(new Event("focus"));
    });
    await waitFor(() =>
      expect(screen.getByTestId("summary")).toHaveAttribute("data-count", "1"),
    );
    // Bell entry present (count 1) but NO toast — you're already looking at it.
    expect(toastFns.success).not.toHaveBeenCalled();
  });

  it("mark-all-read POSTs read-all and is optimistic", async () => {
    currentRows = [row("n1", "run_terminal", "abc")];
    renderProvider();
    await waitFor(() =>
      expect(screen.getByTestId("summary")).toHaveAttribute("data-unread", "1"),
    );
    act(() => {
      screen.getByText("all").click();
    });
    await waitFor(() =>
      expect(screen.getByTestId("summary")).toHaveAttribute("data-unread", "0"),
    );
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("/v1/me/notifications/read-all"),
      expect.objectContaining({ method: "POST" }),
    );
  });

  // R9-003: title resolution must NEVER crash the shell over one malformed row.
  // next-intl surfaces a missing interpolation param as a FORMATTING_ERROR
  // (IntlError via onError + key-echo fallback with the default config, or a
  // throw with a rethrowing onError) — either way the row must render the
  // persona-scoped generic title, never break the provider render.
  describe("malformed-row resilience (R9-003)", () => {
    it("renders the generic title for a schedule_fired row missing `subject` (no throw)", async () => {
      // next-intl's default onError logs the IntlError — silence the expected noise.
      const consoleError = vi
        .spyOn(console, "error")
        .mockImplementation(() => {});
      currentRows = [
        row("s1", "schedule_fired", "sched-1", {
          message_key: "notifications.schedule.fired",
          params: { persona: "Ada" }, // pre-subject-threading row: no `subject`
        }),
        row("n2", "persona_ready", "p9"),
      ];
      renderProvider();
      // The provider render survives the malformed row AND the sibling row still lands.
      await waitFor(() =>
        expect(screen.getByTestId("summary")).toHaveAttribute(
          "data-count",
          "2",
        ),
      );
      expect(screen.getByTestId("entry-s1")).toHaveAttribute(
        "data-title",
        "Ada has an update",
      );
      expect(screen.getByTestId("entry-s1")).toHaveAttribute(
        "data-href",
        "/schedule",
      );
      expect(screen.getByTestId("entry-n2")).toHaveAttribute(
        "data-title",
        "Ada is ready",
      );
      consoleError.mockRestore();
    });

    it("renders the real reminder title when `subject` is present", async () => {
      currentRows = [
        row("s2", "schedule_fired", "sched-2", {
          message_key: "notifications.schedule.fired",
          params: { persona: "Ada", subject: "water the plants" },
        }),
      ];
      renderProvider();
      await waitFor(() =>
        expect(screen.getByTestId("summary")).toHaveAttribute(
          "data-count",
          "1",
        ),
      );
      expect(screen.getByTestId("entry-s2")).toHaveAttribute(
        "data-title",
        "Ada ran your reminder: water the plants",
      );
    });

    it("falls back to the generic title for an unknown message_key", async () => {
      const consoleError = vi
        .spyOn(console, "error")
        .mockImplementation(() => {});
      currentRows = [
        row("u1", "run_terminal", "abc", {
          message_key: "notifications.some.unknown.key",
        }),
      ];
      renderProvider();
      await waitFor(() =>
        expect(screen.getByTestId("summary")).toHaveAttribute(
          "data-count",
          "1",
        ),
      );
      expect(screen.getByTestId("entry-u1")).toHaveAttribute(
        "data-title",
        "Ada has an update",
      );
      consoleError.mockRestore();
    });
  });
});
