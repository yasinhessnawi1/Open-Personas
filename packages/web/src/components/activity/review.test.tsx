/**
 * Spec A6 (W5) — <Review> tests: the five-test calm rubric, made concrete.
 *
 * - **Waiting-first** — sections render in the fixed priority order (waiting → stuck → done →
 *   initiatives).
 * - **Loud only where it informs** — only the stuck section renders the red-rail card.
 * - **Under a minute** — an over-cap section shows an honest "+N more", not a wall.
 * - **Truthful state** — an empty digest reads "a quiet night"; A7 provenance renders when present.
 */
import { act, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { beforeEach, describe, expect, it, vi } from "vitest";

import messages from "@/i18n/messages/en.json";
import type { MorningDigest } from "@/lib/api/review-client";
import { emitTaskSignal } from "@/lib/task-signal";

import { Review } from "./review";

const api = vi.hoisted(() => ({ fetchReview: vi.fn() }));
const auth = vi.hoisted(() => ({
  getToken: () => Promise.resolve("test-token"),
}));

vi.mock("@/auth", () => ({ useAuth: () => ({ getToken: auth.getToken }) }));
vi.mock("@/lib/api/review-client", () => ({ fetchReview: api.fetchReview }));

function digest(overrides: Partial<MorningDigest> = {}): MorningDigest {
  return {
    generated_at: "2026-07-07T07:00:00Z",
    sections: [],
    upcoming: [],
    total_spent_micros: 0,
    persona_names: { kai: "Kai", iris: "Iris" },
    ...overrides,
  };
}

function item(over: Partial<MorningDigest["sections"][0]["items"][0]> = {}) {
  return {
    persona_id: "kai",
    title: "the thing",
    detail: "",
    ref: null,
    ran_because: null,
    ...over,
  };
}

function renderReview() {
  return render(
    <NextIntlClientProvider locale="en" messages={messages}>
      <Review />
    </NextIntlClientProvider>,
  );
}

beforeEach(() => vi.clearAllMocks());

describe("Review", () => {
  it("renders sections waiting-first and makes only stuck loud", async () => {
    api.fetchReview.mockResolvedValue(
      digest({
        sections: [
          {
            kind: "waiting",
            items: [item({ title: "approve the email" })],
            overflow: 0,
          },
          {
            kind: "stuck",
            items: [
              item({ title: "book dentist", detail: "needs your login" }),
            ],
            overflow: 0,
          },
          {
            kind: "done",
            items: [item({ title: "filed receipts" })],
            overflow: 3,
          },
          {
            kind: "initiatives",
            items: [item({ title: "noticed a renewal" })],
            overflow: 0,
          },
        ],
      }),
    );
    renderReview();

    await screen.findByText("approve the email");
    const kinds = Array.from(
      document.querySelectorAll("[data-slot='review-section']"),
    ).map((el) => el.getAttribute("data-kind"));
    expect(kinds).toEqual(["waiting", "stuck", "done", "initiatives"]); // waiting-first

    // loud only where it informs: exactly one red-rail card, in the stuck section.
    expect(
      document.querySelectorAll("[data-slot='review-stuck']"),
    ).toHaveLength(1);
    expect(screen.getByText("needs your login")).toBeInTheDocument();
    // under a minute: the over-cap done section is honest about the remainder.
    expect(screen.getByText("+3 more")).toBeInTheDocument();
  });

  it("reads 'a quiet night' when nothing happened (truthful state)", async () => {
    api.fetchReview.mockResolvedValue(digest());
    renderReview();
    expect(await screen.findByText("A quiet night")).toBeInTheDocument();
  });

  it("deep-links a stuck item to its own task (DigestItem.ref, criterion-10)", async () => {
    api.fetchReview.mockResolvedValue(
      digest({
        sections: [
          {
            kind: "stuck",
            items: [
              item({
                title: "book dentist",
                ref: { kind: "task", id: "t_stuck" },
              }),
            ],
            overflow: 0,
          },
        ],
      }),
    );
    renderReview();
    const link = await screen.findByRole("link", { name: /resolve/i });
    expect(link.getAttribute("href")).toBe("/tasks/t_stuck");
  });

  it("refetches the durable digest on a task.updated signal (W8, refetch-not-trust)", async () => {
    api.fetchReview.mockResolvedValue(digest());
    renderReview();
    await screen.findByText("A quiet night");
    expect(api.fetchReview).toHaveBeenCalledTimes(1);
    act(() => emitTaskSignal({ id: "s1", taskId: "t1" }));
    await waitFor(() => expect(api.fetchReview).toHaveBeenCalledTimes(2));
  });

  it("renders A7 'ran because' provenance when present (the W8 wiring target)", async () => {
    api.fetchReview.mockResolvedValue(
      digest({
        sections: [
          {
            kind: "done",
            items: [
              item({
                title: "sent the reminder",
                ran_because: "ran because: your invoice fell due",
              }),
            ],
            overflow: 0,
          },
        ],
      }),
    );
    renderReview();
    expect(
      await screen.findByText("ran because: your invoice fell due"),
    ).toBeInTheDocument();
  });
});
