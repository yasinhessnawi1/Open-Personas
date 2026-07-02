/**
 * Spec K6 (T5) — the optional name nudge.
 *
 * Gated on null: shown only when our DB has no name for the caller; hidden when a
 * name is already set (incl. a Clerk-seeded one). Save PATCHes /v1/me/profile with
 * the typed name; Skip dismisses without a PATCH (no dark pattern, no re-nag).
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import messages from "@/i18n/messages/en.json";
import { NameNudge } from "./name-nudge";

vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ getToken: () => Promise.resolve("jwt") }),
}));

interface Captured {
  url: string;
  method: string;
  body: string;
}

function installFetch(profile: {
  first_name: string | null;
  last_name: string | null;
}): { captured: Captured[]; restore: () => void } {
  const captured: Captured[] = [];
  const original = globalThis.fetch;
  globalThis.fetch = vi.fn(
    async (input: string | URL | Request, init?: RequestInit) => {
      const req =
        typeof input === "object" && "method" in input
          ? (input as Request)
          : null;
      const url = req ? req.url : input.toString();
      const method = init?.method ?? req?.method ?? "GET";
      let body = typeof init?.body === "string" ? init.body : "";
      if (!body && req) body = await req.clone().text();
      captured.push({ url, method, body });
      if (url.includes("/v1/me/profile")) {
        const base = {
          id: "u1",
          email: "u1@x",
          created_at: "2026-07-02T00:00:00Z",
        };
        if (method === "PATCH") {
          const patch = JSON.parse(body) as Record<string, string | null>;
          return new Response(
            JSON.stringify({ ...base, ...profile, ...patch }),
            {
              status: 200,
              headers: { "Content-Type": "application/json" },
            },
          );
        }
        return new Response(JSON.stringify({ ...base, ...profile }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      return new Response(null, { status: 204 });
    },
  ) as unknown as typeof fetch;
  return {
    captured,
    restore: () => {
      globalThis.fetch = original;
    },
  };
}

function renderNudge() {
  return render(
    <NextIntlClientProvider locale="en" messages={messages}>
      <NameNudge />
    </NextIntlClientProvider>,
  );
}

describe("NameNudge (Spec K6 T5)", () => {
  let restore: () => void;

  beforeEach(() => {
    sessionStorage.clear();
  });
  afterEach(() => {
    restore?.();
    vi.restoreAllMocks();
  });

  it("shows when our DB has no name", async () => {
    ({ restore } = installFetch({ first_name: null, last_name: null }));
    renderNudge();
    expect(
      await screen.findByText("What should we call you?"),
    ).toBeInTheDocument();
  });

  it("stays hidden when a name is already set (never nags a named/seeded user)", async () => {
    ({ restore } = installFetch({ first_name: "Ada", last_name: "Lovelace" }));
    renderNudge();
    // Give the mount effect a tick to resolve, then assert it never appeared.
    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalled());
    expect(
      screen.queryByText("What should we call you?"),
    ).not.toBeInTheDocument();
  });

  it("Save PATCHes /v1/me/profile with the typed name, then dismisses", async () => {
    const { captured, restore: r } = installFetch({
      first_name: null,
      last_name: null,
    });
    restore = r;
    renderNudge();
    await screen.findByText("What should we call you?");

    fireEvent.change(screen.getByLabelText("First name"), {
      target: { value: "Ada" },
    });
    fireEvent.change(screen.getByLabelText("Last name"), {
      target: { value: "Lovelace" },
    });
    fireEvent.click(screen.getByText("Save"));

    await waitFor(() =>
      expect(
        screen.queryByText("What should we call you?"),
      ).not.toBeInTheDocument(),
    );
    const patch = captured.find((c) => c.method === "PATCH");
    expect(patch).toBeDefined();
    expect(JSON.parse(patch?.body ?? "{}")).toEqual({
      first_name: "Ada",
      last_name: "Lovelace",
    });
    expect(sessionStorage.getItem("op.nameNudge.dismissed")).toBe("1");
  });

  it("Skip dismisses without any PATCH", async () => {
    const { captured, restore: r } = installFetch({
      first_name: null,
      last_name: null,
    });
    restore = r;
    renderNudge();
    await screen.findByText("What should we call you?");

    fireEvent.click(screen.getByText("Not now"));

    await waitFor(() =>
      expect(
        screen.queryByText("What should we call you?"),
      ).not.toBeInTheDocument(),
    );
    expect(captured.some((c) => c.method === "PATCH")).toBe(false);
    expect(sessionStorage.getItem("op.nameNudge.dismissed")).toBe("1");
  });
});
