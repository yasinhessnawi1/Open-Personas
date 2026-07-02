/**
 * Spec 35 cluster L (D-35-11) — the notification bell + center.
 *
 * Verifies: an unread badge appears once notifications land; opening the panel
 * lists the feed entries + marks them read (badge clears); empty state renders
 * when there's nothing; "Clear all" empties the feed.
 */

import { act, fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/components/patterns/toast", () => ({
  toast: { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() },
}));

import {
  NotificationProvider,
  useNotify,
} from "@/components/providers/notification-provider";
import { NotificationBell } from "./notification-bell";

const messages = {
  notifications: {
    open: "Notifications",
    title: "Notifications",
    empty: "Nothing yet.",
    clear: "Clear all",
    unreadLabel: "{count} unread",
    deleted: "{name} deleted",
    duplicated: "{name} duplicated",
  },
};

/** A button that fires a notify so tests can seed the feed via the real provider. */
function Seeder() {
  const { notify } = useNotify();
  return (
    <button
      type="button"
      onClick={() => notify({ level: "success", title: "Persona deleted" })}
    >
      seed
    </button>
  );
}

/** Seeds an entry carrying a deep-link href. */
function HrefSeeder() {
  const { notify } = useNotify();
  return (
    <button
      type="button"
      onClick={() =>
        notify({ level: "success", title: "Task finished", href: "/runs/abc" })
      }
    >
      seed-href
    </button>
  );
}

function renderBell(children?: ReactNode) {
  return render(
    <NextIntlClientProvider locale="en" messages={messages}>
      <NotificationProvider>
        <NotificationBell />
        {children}
      </NotificationProvider>
    </NextIntlClientProvider>,
  );
}

beforeEach(() => {
  window.localStorage.clear();
  vi.clearAllMocks();
});

describe("NotificationBell", () => {
  it("shows no unread badge when the feed is empty", () => {
    const { container } = renderBell();
    expect(
      container.querySelector('[data-slot="notification-unread"]'),
    ).toBeNull();
  });

  it("shows an unread badge after a notification lands", () => {
    const { container } = renderBell(<Seeder />);
    act(() => {
      fireEvent.click(screen.getByText("seed"));
    });
    const badge = container.querySelector('[data-slot="notification-unread"]');
    expect(badge).not.toBeNull();
    expect(badge).toHaveTextContent("1");
  });

  it("opening the panel lists entries and clears the unread badge", () => {
    const { container } = renderBell(<Seeder />);
    act(() => {
      fireEvent.click(screen.getByText("seed"));
    });
    // Open the bell.
    fireEvent.click(screen.getByRole("button", { name: "Notifications" }));
    expect(screen.getByText("Persona deleted")).toBeInTheDocument();
    // Opening marks read → the unread badge is gone.
    expect(
      container.querySelector('[data-slot="notification-unread"]'),
    ).toBeNull();
  });

  it("renders an entry with href as a deep-link to its target", () => {
    renderBell(<HrefSeeder />);
    act(() => {
      fireEvent.click(screen.getByText("seed-href"));
    });
    fireEvent.click(screen.getByRole("button", { name: "Notifications" }));
    const link = screen.getByRole("link", { name: /Task finished/ });
    expect(link).toHaveAttribute("href", "/runs/abc");
  });

  it("renders an entry without href as non-interactive text (no link)", () => {
    renderBell(<Seeder />);
    act(() => {
      fireEvent.click(screen.getByText("seed"));
    });
    fireEvent.click(screen.getByRole("button", { name: "Notifications" }));
    expect(screen.getByText("Persona deleted")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Persona deleted/ })).toBeNull();
  });

  it("Clear all empties the feed", () => {
    renderBell(<Seeder />);
    act(() => {
      fireEvent.click(screen.getByText("seed"));
    });
    fireEvent.click(screen.getByRole("button", { name: "Notifications" }));
    fireEvent.click(screen.getByText("Clear all"));
    expect(screen.getByText("Nothing yet.")).toBeInTheDocument();
  });
});
