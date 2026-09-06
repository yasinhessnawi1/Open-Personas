/**
 * Spec P6 (P6-D-6) — low-balance-at-load watcher.
 *
 * Proves the source fires against the REAL `/v1/me/credits` read (the transport
 * boundary is mocked, not the notify): a low balance emits a persisted warning
 * deep-linking to billing, exactly once per session; a healthy or zero balance
 * does not.
 */

import { render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { beforeEach, describe, expect, it, vi } from "vitest";

const getCredits = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api/client")>();
  return { ...actual, createApiClient: () => ({ GET: getCredits }) };
});
vi.mock("@/auth", () => ({
  useAuth: () => ({ getToken: vi.fn().mockResolvedValue("jwt-token") }),
}));
const toastFns = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
  info: vi.fn(),
  warning: vi.fn(),
}));
vi.mock("@/components/patterns/toast", () => ({ toast: toastFns }));

import {
  NotificationProvider,
  useNotify,
} from "@/components/providers/notification-provider";
import { LowBalanceWatcher } from "./low-balance-watcher";

const messages = {
  notifications: {
    lowBalance: { title: "Low balance", body: "{count} credits left" },
  },
};

/** Surfaces the feed's first entry so tests can assert persist + href + level. */
function Probe() {
  const { entries } = useNotify();
  const e = entries[0];
  return (
    <div
      data-testid="probe"
      data-count={entries.length}
      data-href={e?.href ?? ""}
      data-level={e?.level ?? ""}
    />
  );
}

function renderWatcher() {
  return render(
    <NextIntlClientProvider locale="en" messages={messages}>
      <NotificationProvider>
        <LowBalanceWatcher />
        <Probe />
      </NotificationProvider>
    </NextIntlClientProvider>,
  );
}

function creditsReply(balance: number, low_balance: boolean) {
  return {
    data: { balance, low_balance },
    error: undefined,
    response: new Response(null, { status: 200 }),
  };
}

beforeEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
  vi.clearAllMocks();
});

describe("LowBalanceWatcher", () => {
  it("notifies once with a billing deep-link when the balance is low", async () => {
    getCredits.mockResolvedValue(creditsReply(500, true));
    renderWatcher();
    await waitFor(() =>
      expect(screen.getByTestId("probe")).toHaveAttribute("data-count", "1"),
    );
    const probe = screen.getByTestId("probe");
    expect(probe).toHaveAttribute("data-level", "warning");
    // Spec M5 (T6): retargeted from `/settings`, which only SHOWS the balance, to
    // `/settings/billing`, where credits can actually be bought. Warning a user and
    // sending them somewhere that cannot fix it is the dead end D-M5-7 closes, so
    // this assertion is what keeps the link pointed at a page that resolves it.
    expect(probe).toHaveAttribute("data-href", "/settings/billing");
    expect(toastFns.warning).toHaveBeenCalledTimes(1);
    // It read the REAL endpoint, not a static trigger.
    expect(getCredits).toHaveBeenCalledWith("/v1/me/credits");
  });

  it("does not notify when the balance is healthy", async () => {
    getCredits.mockResolvedValue(creditsReply(50_000, false));
    renderWatcher();
    await waitFor(() => expect(getCredits).toHaveBeenCalled());
    expect(screen.getByTestId("probe")).toHaveAttribute("data-count", "0");
    expect(toastFns.warning).not.toHaveBeenCalled();
  });

  it("does not notify at zero balance (the 402 cliff is a separate surface)", async () => {
    getCredits.mockResolvedValue(creditsReply(0, true));
    renderWatcher();
    await waitFor(() => expect(getCredits).toHaveBeenCalled());
    expect(screen.getByTestId("probe")).toHaveAttribute("data-count", "0");
    expect(toastFns.warning).not.toHaveBeenCalled();
  });

  it("fires at most once per session (sessionStorage guard already set)", async () => {
    window.sessionStorage.setItem("open-persona:low-balance-notified", "1");
    getCredits.mockResolvedValue(creditsReply(500, true));
    renderWatcher();
    // Guarded before any fetch — the endpoint isn't even read again.
    await new Promise((r) => setTimeout(r, 0));
    expect(getCredits).not.toHaveBeenCalled();
    expect(screen.getByTestId("probe")).toHaveAttribute("data-count", "0");
    expect(toastFns.warning).not.toHaveBeenCalled();
  });
});
