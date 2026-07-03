import { act, renderHook } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import en from "@/i18n/messages/en.json";
import { CONNECTOR_CATALOGUE } from "./catalogue";
import type { ConnectorConnection } from "./use-connectors";

/**
 * Spec C6 (T9) — disconnect: see-then-sever (a concrete danger confirm), then C1's real unlink
 * via DELETE. Severed state comes from the refetch (sole oracle, never optimistic); a failed
 * DELETE leaves the card untouched with an honest error voice (no phantom sever).
 */

const confirmMock = vi.hoisted(() => vi.fn());
const toastSpies = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
  warning: vi.fn(),
  info: vi.fn(),
}));
const deleteMock = vi.hoisted(() => vi.fn());

vi.mock("@/components/providers/confirm-provider", () => ({
  useConfirm: () => confirmMock,
}));
vi.mock("@/components/patterns/toast", () => ({ useToast: () => toastSpies }));
vi.mock("@/lib/api/use-api", () => ({
  useApi: () => ({ DELETE: deleteMock }),
}));

import { useDisconnect } from "./use-disconnect";

const DISCORD = CONNECTOR_CATALOGUE.find((m) => m.key === "discord");
if (!DISCORD) throw new Error("discord missing from catalogue");
const CONN: ConnectorConnection = {
  platform: "discord",
  platform_identity: "998877",
  linked_at: "2026-07-01T00:00:00Z",
};

const wrapper = ({ children }: { children: React.ReactNode }) => (
  <NextIntlClientProvider locale="en" messages={en}>
    {children}
  </NextIntlClientProvider>
);

function ok() {
  return {
    data: { severed: true },
    response: { status: 200, headers: new Headers() },
  };
}
function fail() {
  return {
    error: { error: "server_error" },
    response: { status: 500, headers: new Headers() },
  };
}

beforeEach(() => {
  confirmMock.mockReset();
  deleteMock.mockReset();
  toastSpies.success.mockClear();
  toastSpies.error.mockClear();
});
afterEach(() => vi.restoreAllMocks());

describe("useDisconnect", () => {
  it("confirms with the concrete consequence — platform + identity + what stops (bar 1)", async () => {
    confirmMock.mockResolvedValue(false); // cancel — we only inspect the prompt here
    const refresh = vi.fn().mockResolvedValue([]);
    const { result } = renderHook(() => useDisconnect(refresh), { wrapper });
    await act(async () => {
      await result.current(DISCORD, CONN);
    });
    const opts = confirmMock.mock.calls[0][0];
    expect(opts.title).toContain("Discord");
    expect(opts.description).toContain("ID 998877"); // the honest identity being severed
    expect(opts.description).toMatch(/no longer be reachable/);
    expect(opts.tone).toBe("danger");
  });

  it("on confirm: DELETEs the exact binding, refetches (sole oracle), confirms", async () => {
    confirmMock.mockResolvedValue(true);
    deleteMock.mockResolvedValue(ok());
    const refresh = vi.fn().mockResolvedValue([]);
    const { result } = renderHook(() => useDisconnect(refresh), { wrapper });
    await act(async () => {
      await result.current(DISCORD, CONN);
    });
    expect(deleteMock).toHaveBeenCalledWith(
      "/v1/me/connectors/{platform}/{platform_identity}",
      {
        params: { path: { platform: "discord", platform_identity: "998877" } },
      },
    );
    expect(refresh).toHaveBeenCalled(); // severed state comes from the refetch, not optimism
    expect(toastSpies.success).toHaveBeenCalled();
  });

  it("cancel: no DELETE, no refetch (one confirm gates everything)", async () => {
    confirmMock.mockResolvedValue(false);
    const refresh = vi.fn();
    const { result } = renderHook(() => useDisconnect(refresh), { wrapper });
    await act(async () => {
      await result.current(DISCORD, CONN);
    });
    expect(deleteMock).not.toHaveBeenCalled();
    expect(refresh).not.toHaveBeenCalled();
  });

  it("failure: honest error voice, NO refetch, no phantom sever (bar 3)", async () => {
    confirmMock.mockResolvedValue(true);
    deleteMock.mockResolvedValue(fail());
    const refresh = vi.fn();
    const { result } = renderHook(() => useDisconnect(refresh), { wrapper });
    await act(async () => {
      await result.current(DISCORD, CONN);
    });
    expect(deleteMock).toHaveBeenCalled();
    expect(toastSpies.error).toHaveBeenCalled();
    expect(toastSpies.success).not.toHaveBeenCalled();
    // The list is NOT refetched on failure → the card stays connected (no phantom sever).
    expect(refresh).not.toHaveBeenCalled();
  });
});
