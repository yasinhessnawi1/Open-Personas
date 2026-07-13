import { render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import en from "@/i18n/messages/en.json";

/**
 * Spec C6 (T7) — the OAuth 302-return, adversarial. A forged/stale `?result=connected` with
 * NO binding in the list must NOT render as connected anywhere (C6-D-2): the cards come from
 * the list, and even the toast is "unconfirmed" (not success). The URL is stripped so a
 * reload/back never re-toasts, and it all works on a cold page load (zero modal state).
 */

const toastSpies = vi.hoisted(() => ({
  success: vi.fn(),
  warning: vi.fn(),
  info: vi.fn(),
  error: vi.fn(),
}));
const refreshMock = vi.hoisted(() => vi.fn());

vi.mock("@/components/patterns/toast", () => ({ useToast: () => toastSpies }));
// The manager also wires disconnect (T9) → useConfirm/useApi; stub them (not exercised here).
vi.mock("@/components/providers/confirm-provider", () => ({
  useConfirm: () => vi.fn(),
}));
vi.mock("@/lib/api/use-api", () => ({
  useApi: () => ({ POST: vi.fn(), DELETE: vi.fn() }),
}));
vi.mock("@/lib/connectors/use-connectors", () => ({
  useConnectors: () => ({
    connections: [],
    loading: false,
    error: null,
    refresh: refreshMock,
  }),
}));

// Imported AFTER the mocks so the manager binds the mocked hooks.
import { ConnectorsManager } from "./connectors-manager";

function renderWithIntl() {
  return render(
    <NextIntlClientProvider locale="en" messages={en}>
      <ConnectorsManager />
    </NextIntlClientProvider>,
  );
}

describe("ConnectorsManager — OAuth return (adversarial)", () => {
  beforeEach(() => {
    toastSpies.success.mockClear();
    toastSpies.warning.mockClear();
    refreshMock.mockReset();
    refreshMock.mockResolvedValue([]); // the list is empty — no binding landed
    window.history.pushState(
      null,
      "",
      "/connectors?result=connected&platform=discord",
    );
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("a forged result=connected with no binding renders NOTHING as connected + honest voice", async () => {
    const replaceSpy = vi.spyOn(window.history, "replaceState");
    renderWithIntl();

    // No card shows a connected state — the surface reflects the (empty) list, not the param.
    expect(screen.queryByText("Connected")).not.toBeInTheDocument();
    const discordCard = document.querySelector('[data-platform="discord"]');
    expect(discordCard).toHaveAttribute("data-connected", "false");

    // The toast keys off the refreshed list: no binding → "unconfirmed", never success.
    await waitFor(() => expect(toastSpies.warning).toHaveBeenCalledTimes(1));
    expect(toastSpies.success).not.toHaveBeenCalled();
    expect(refreshMock).toHaveBeenCalled();

    // The URL is stripped so a reload / back never re-toasts (single-consume).
    expect(replaceSpy).toHaveBeenCalledWith(null, "", "/connectors");
  });
});
