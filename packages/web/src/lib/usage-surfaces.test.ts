import { describe, expect, it } from "vitest";
import {
  aggregateBySurface,
  creditShare,
  type LedgerEntry,
  SURFACE_ORDER,
  surfaceKey,
  totalsOf,
} from "./usage-surfaces";

function entry(
  partial: Partial<LedgerEntry> & { reason: string },
): LedgerEntry {
  return {
    delta: -10,
    cost_cents: 1.0,
    cost_basis: "actual_openrouter",
    created_at: "2026-07-18T00:00:00Z",
    ...partial,
  };
}

describe("surfaceKey", () => {
  it("maps every real backend reason prefix to a bucket", () => {
    // Direct charge sites (grepped from the API/runtime source).
    expect(surfaceKey("chat")).toBe("chat");
    expect(surfaceKey("chat:actual_openrouter")).toBe("chat");
    expect(surfaceKey("persona_authoring")).toBe("authoring");
    expect(surfaceKey("persona_authoring_refine")).toBe("authoring");
    expect(surfaceKey("authoring:estimate_static")).toBe("authoring");
    expect(surfaceKey("image_gen:actual_openrouter")).toBe("image");
    expect(surfaceKey("image_gen_pre")).toBe("image");
    expect(surfaceKey("image_gen_trueup:actual_openrouter")).toBe("image");
    expect(surfaceKey("avatar_gen:estimate_static")).toBe("image");
    expect(surfaceKey("voice:provider_meter")).toBe("voice");
    expect(surfaceKey("voice:infra_flat")).toBe("voice");
    expect(surfaceKey("voice_stt:provider_meter")).toBe("voice");
    expect(surfaceKey("voice_tts:provider_meter")).toBe("voice");
    expect(surfaceKey("voice_transport:infra_flat")).toBe("voice");
    expect(surfaceKey("agentic_run:estimate_static")).toBe("agentic");
    expect(surfaceKey("task_leg:actual_openrouter")).toBe("task");
    expect(surfaceKey("sandbox:infra_flat")).toBe("sandbox");
    // Background LLM — including voice-autopick (voice_pick), per the backend
    // LedgerEntry docstring which buckets it with episodic + initiative.
    expect(surfaceKey("voice_pick:estimate_static")).toBe("background");
    expect(surfaceKey("episodic_consolidation:estimate_static")).toBe(
      "background",
    );
    expect(surfaceKey("initiative_scan:estimate_static")).toBe("background");
    expect(surfaceKey("embeddings:actual_openrouter")).toBe("background");
    expect(surfaceKey("connectors_mcp:infra_flat")).toBe("background");
  });

  it("falls back to 'other' for unknown reasons", () => {
    expect(surfaceKey("something_new:basis")).toBe("other");
    expect(surfaceKey("")).toBe("other");
  });
});

describe("aggregateBySurface", () => {
  it("groups multiple surfaces, ordered, only non-empty ones", () => {
    const groups = aggregateBySurface([
      entry({ reason: "chat:actual_openrouter", delta: -5, cost_cents: 0.5 }),
      entry({ reason: "chat:actual_openrouter", delta: -7, cost_cents: 0.7 }),
      entry({
        reason: "image_gen:actual_openrouter",
        delta: -40,
        cost_cents: 4,
      }),
      entry({ reason: "sandbox:infra_flat", delta: -2, cost_cents: 0 }),
      entry({
        reason: "voice_pick:estimate_static",
        delta: -1,
        cost_cents: 0.1,
      }),
    ]);

    expect(groups.map((g) => g.key)).toEqual([
      "chat",
      "image",
      "background",
      "sandbox",
    ]);
    const chat = groups.find((g) => g.key === "chat");
    expect(chat?.count).toBe(2);
    expect(chat?.creditsCharged).toBe(12); // 5 + 7
    expect(chat?.costCents).toBeCloseTo(1.2);
    expect(chat?.basis).toBe("actual_openrouter");
  });

  it("nets refunds against charges (positive delta reduces spend)", () => {
    const groups = aggregateBySurface([
      entry({ reason: "image_gen:actual_openrouter", delta: -40 }),
      entry({ reason: "image_gen_refund:backend_failure", delta: 40 }),
    ]);
    expect(groups[0]?.key).toBe("image");
    expect(groups[0]?.creditsCharged).toBe(0);
  });

  it("marks mixed provenance and skips null bases / unpriced cost", () => {
    const groups = aggregateBySurface([
      entry({ reason: "voice:provider_meter", cost_basis: "provider_meter" }),
      entry({ reason: "voice:infra_flat", cost_basis: "infra_flat" }),
      entry({ reason: "chat", cost_basis: null, cost_cents: null }),
    ]);
    const voice = groups.find((g) => g.key === "voice");
    expect(voice?.basis).toBe("mixed");
    const chat = groups.find((g) => g.key === "chat");
    expect(chat?.basis).toBeNull();
    expect(chat?.hasCost).toBe(false);
  });

  it("returns [] for an empty ledger (community edition)", () => {
    expect(aggregateBySurface([])).toEqual([]);
  });
});

describe("totalsOf + creditShare", () => {
  it("sums groups and computes a share that guards a zero total", () => {
    const groups = aggregateBySurface([
      entry({ reason: "chat", delta: -30, cost_cents: 3 }),
      entry({ reason: "image_gen", delta: -70, cost_cents: 7 }),
    ]);
    const total = totalsOf(groups);
    expect(total.creditsCharged).toBe(100);
    expect(total.costCents).toBeCloseTo(10);
    expect(total.count).toBe(2);

    const image = groups.find((g) => g.key === "image");
    expect(image && creditShare(image, total)).toBeCloseTo(0.7);

    const zero = totalsOf([]);
    expect(
      creditShare(
        {
          key: "chat",
          count: 0,
          creditsCharged: 0,
          costCents: 0,
          hasCost: false,
          basis: null,
        },
        zero,
      ),
    ).toBe(0);
  });
});

describe("SURFACE_ORDER", () => {
  it("covers every SurfaceKey exactly once", () => {
    expect(new Set(SURFACE_ORDER).size).toBe(SURFACE_ORDER.length);
    expect(SURFACE_ORDER).toHaveLength(9);
  });
});
