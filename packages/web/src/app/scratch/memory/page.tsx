"use client";

/**
 * Spec K5 — Memory canvas dev harness (NODE_ENV-guarded).
 *
 * The real `/memory` page only mounts the force-graph when a graph store is
 * available AND has nodes — which needs the cloud Postgres + auth + seeded data
 * stack (the K5-R-4 operator/Playwright leg). This harness mounts the REAL
 * `MemoryView` with a ~30-node mock window (design-density) + a pre-seeded detail
 * so the graphology worker layout, the 2-D canvas, and the editorial panel
 * (correction + delete) all bundle and paint in the running dev app — the
 * bundling/mount + design-match evidence `tsc` can't produce. Production throws.
 */

import { useEffect, useState } from "react";
import { MemoryView } from "@/components/memory/memory-view";
import { ToastProvider } from "@/components/patterns/toast";
import { ConfirmProvider } from "@/components/providers/confirm-provider";
import { NotificationProvider } from "@/components/providers/notification-provider";
import type {
  MemoryLinkEdge,
  MemoryNodeDetail,
  MemoryNodeSummary,
  MemoryWindowResponse,
} from "@/lib/api";

if (process.env.NODE_ENV === "production") {
  throw new Error(
    "scratch/memory is a dev-only harness (K5 canvas bundling + design-match check).",
  );
}

type Kind = MemoryNodeSummary["kind"];
const n = (
  id: string,
  kind: Kind,
  label: string,
  degree: number,
  sensitive = false,
): MemoryNodeSummary => ({
  id,
  kind,
  label,
  degree,
  wellbeing_category: sensitive ? "distress" : null,
});
const e = (
  src_node_id: string,
  dst_node_id: string,
  link_type: MemoryLinkEdge["link_type"],
): MemoryLinkEdge => ({ src_node_id, dst_node_id, link_type, weight: null });

const NODES: MemoryNodeSummary[] = [
  // self / identity core
  n("you", "trait", "You", 8),
  n("concise", "preference", "Prefers concise answers", 2),
  n("sources", "preference", "Wants sources for claims", 3),
  n("norwegian", "trait", "Speaks Norwegian & English", 2),
  n("morning", "trait", "Works best in the morning", 2),
  n("risk_averse", "preference", "Cautious with money & legal", 3),
  // the move to Oslo
  n("move", "circumstance", "Moved to Oslo", 7),
  n("oslo", "entity", "Oslo", 4),
  n("flat", "entity", "Grünerløkka flat", 3),
  n("new_job", "circumstance", "Started a new job", 3),
  // tenancy & deposit
  n("deposit", "circumstance", "Deposit dispute with landlord", 5),
  n("husleie", "fact", "Knows husleieloven §3-5", 3),
  n("landlord", "entity", "Landlord (Grünerløkka)", 3),
  n("htu", "circumstance", "Filed with Husleietvistutvalget", 2),
  n("mediation", "preference", "Prefers mediation over court", 2),
  // battery research
  n("battery", "goal", "Researching grid batteries", 4),
  n("lfp", "fact", "Favours LFP chemistry", 3),
  n("survey", "goal", "Writing a battery survey", 3),
  n("primary_src", "preference", "Values primary sources", 4),
  // writing
  n("novel", "goal", "Writing a novel", 4),
  n("voice_style", "preference", "Spare, rhythmic prose voice", 2),
  n("chapter3", "circumstance", "Stuck on chapter 3", 2),
  // work & code
  n("codebase", "circumstance", "Works on a Python API", 5),
  n("pkce", "concept", "Cares about auth security (PKCE)", 2),
  n("review_style", "preference", "Wants risk flagged before style", 3),
  // people
  n("ingrid", "entity", "Partner — Ingrid", 2),
  n("sister", "entity", "Sister in Bergen", 2),
  n("tom", "entity", "Colleague — Tom", 2),
  // wellbeing (K4-marked)
  n("stress", "circumstance", "Felt stressed during the move", 3, true),
  n("sleep", "circumstance", "Sleep affected by deadlines", 2, true),
  n("health", "fact", "Mentioned a back injury", 1, true),
];

const LINKS: MemoryLinkEdge[] = [
  // entity threads (same thing) — gold solid
  e("move", "oslo", "entity"),
  e("flat", "oslo", "entity"),
  e("landlord", "flat", "entity"),
  e("deposit", "landlord", "entity"),
  e("ingrid", "you", "entity"),
  e("sister", "you", "entity"),
  e("tom", "codebase", "entity"),
  // causal (led to) — red + arrow
  e("move", "deposit", "causal"),
  e("move", "new_job", "causal"),
  e("new_job", "codebase", "causal"),
  e("move", "stress", "causal"),
  e("deposit", "htu", "causal"),
  e("novel", "chapter3", "causal"),
  e("battery", "survey", "causal"),
  // temporal (before / after) — green dashed
  e("move", "battery", "temporal"),
  e("battery", "lfp", "temporal"),
  e("new_job", "morning", "temporal"),
  e("move", "novel", "temporal"),
  // semantic (related) — faint dotted
  e("you", "concise", "semantic"),
  e("you", "sources", "semantic"),
  e("you", "norwegian", "semantic"),
  e("you", "morning", "semantic"),
  e("you", "risk_averse", "semantic"),
  e("husleie", "deposit", "semantic"),
  e("husleie", "risk_averse", "semantic"),
  e("mediation", "htu", "semantic"),
  e("risk_averse", "mediation", "semantic"),
  e("lfp", "battery", "semantic"),
  e("lfp", "primary_src", "semantic"),
  e("sources", "primary_src", "semantic"),
  e("survey", "primary_src", "semantic"),
  e("voice_style", "novel", "semantic"),
  e("concise", "voice_style", "semantic"),
  e("pkce", "codebase", "semantic"),
  e("review_style", "codebase", "semantic"),
  e("morning", "codebase", "semantic"),
  e("review_style", "sources", "semantic"),
  e("stress", "sleep", "semantic"),
  e("sleep", "health", "semantic"),
  e("stress", "move", "semantic"),
  e("ingrid", "move", "semantic"),
  e("sister", "oslo", "semantic"),
  e("norwegian", "oslo", "semantic"),
];

const MOCK_WINDOW: MemoryWindowResponse = {
  available: true,
  focus_id: null,
  is_seed: true,
  total_nodes: NODES.length,
  nodes: NODES,
  links: LINKS,
};

const summary = (id: string): MemoryNodeSummary =>
  NODES.find((node) => node.id === id) ?? NODES[0];

// A pre-seeded detail so the editorial panel renders fully (correction + delete)
// without a live fetch — the design-match evidence for the trust spine.
const DEMO_DETAIL: MemoryNodeDetail = {
  id: "deposit",
  kind: "circumstance",
  label: "Deposit dispute with landlord",
  content:
    "You are disputing a withheld deposit with your Grünerløkka landlord — specifically a smoke-damage deduction you believe is unfair.",
  wellbeing_category: null,
  created_at: "2026-03-12T10:00:00Z",
  origin: {
    source: "persona_self",
    persona_id: "persona_astrid",
    persona_name: "Astrid",
    interaction_id: "conv_deposit_01",
    written_at: "2026-03-12T10:00:00Z",
    reason: "Came up when you and Astrid worked through the deposit dispute",
    grounding: null,
  },
  evolution: [
    {
      source: "persona_self",
      written_at: "2026-03-12T10:00:00Z",
      reason: "First learned — you described the withheld deposit",
      superseded_content: null,
    },
    {
      source: "persona_self",
      written_at: "2026-03-12T14:00:00Z",
      reason: "Refined — narrowed to the smoke-damage deduction",
      superseded_content: null,
    },
    {
      source: "persona_self",
      written_at: "2026-03-14T09:00:00Z",
      reason: "Connected — linked to your husleieloven question",
      superseded_content: null,
    },
  ],
  links: [
    {
      link_type: "entity",
      direction: "out",
      weight: null,
      neighbor: summary("landlord"),
    },
    {
      link_type: "causal",
      direction: "in",
      weight: null,
      neighbor: summary("move"),
    },
    {
      link_type: "causal",
      direction: "out",
      weight: null,
      neighbor: summary("htu"),
    },
    {
      link_type: "semantic",
      direction: "out",
      weight: null,
      neighbor: summary("husleie"),
    },
  ],
};

export default function ScratchMemoryPage() {
  // Default = the clean graph overview; `?panel` pre-opens the editorial panel.
  const [showPanel, setShowPanel] = useState(false);
  useEffect(() => {
    setShowPanel(new URLSearchParams(window.location.search).has("panel"));
  }, []);
  return (
    <NotificationProvider>
      <ConfirmProvider>
        <div className="flex h-svh flex-col p-4">
          <MemoryView
            key={showPanel ? "panel" : "graph"}
            memoryWindow={MOCK_WINDOW}
            demoDetail={showPanel ? DEMO_DETAIL : undefined}
          />
        </div>
        <ToastProvider />
      </ConfirmProvider>
    </NotificationProvider>
  );
}
