import { getTranslations } from "next-intl/server";
import type { CSSProperties } from "react";
import { cn } from "@/lib/utils";

/**
 * Spec 35 (D-35-1) — the four typed-memory stores made visible (the signature
 * "memory presence"). Each store carries its F1 store-colour token (A) as a 2px
 * left border + badge + bullet dots. identity / self_facts / worldview render
 * the persona's REAL definition; episodic shows its runtime source (the
 * per-turn-recalled conversation count). Server component — presentational.
 */

interface WorldviewItem {
  readonly claim: string;
  readonly epistemic?: string | null;
}

const STORE_COLOR = {
  identity: "var(--store-identity)",
  selfFacts: "var(--store-self-facts)",
  worldview: "var(--store-worldview)",
  episodic: "var(--store-episodic)",
} as const;

export async function MemoryStores({
  name,
  role,
  language,
  selfFacts,
  worldview,
  conversationCount,
}: {
  name: string;
  role: string;
  language: string;
  selfFacts: readonly string[];
  worldview: readonly WorldviewItem[];
  conversationCount: number;
}) {
  const t = await getTranslations("personas.memory");

  return (
    <div className="v-grid v-grid--2" data-slot="memory-stores">
      <StoreCard
        color={STORE_COLOR.identity}
        badge="ID"
        label="identity"
        meta={t("identityMeta")}
        desc={t("identityDesc")}
        mono
        items={[
          `${t("fieldName")}: ${name}`,
          `${t("fieldRole")}: ${role}`,
          `${t("fieldLanguage")}: ${language}`,
        ]}
      />
      <StoreCard
        color={STORE_COLOR.selfFacts}
        badge="SF"
        label="self_facts"
        meta={t("selfFactsMeta")}
        desc={t("selfFactsDesc")}
        emptyLabel={t("none")}
        items={selfFacts}
      />
      <StoreCard
        color={STORE_COLOR.worldview}
        badge="WV"
        label="worldview"
        meta={t("worldviewMeta")}
        desc={t("worldviewDesc")}
        emptyLabel={t("none")}
        items={worldview.map((w) =>
          w.epistemic ? `${w.claim} (${w.epistemic})` : w.claim,
        )}
      />
      <StoreCard
        color={STORE_COLOR.episodic}
        badge="EP"
        label="episodic"
        meta={t("episodicMeta")}
        desc={t("episodicDesc")}
        items={[t("episodicConversations", { count: conversationCount })]}
      />
    </div>
  );
}

function StoreCard({
  color,
  badge,
  label,
  meta,
  desc,
  items,
  mono = false,
  emptyLabel,
}: {
  color: string;
  badge: string;
  label: string;
  meta: string;
  desc: string;
  items: readonly string[];
  mono?: boolean;
  emptyLabel?: string;
}) {
  return (
    <div
      className="v-card v-card--pad border-l-2 transition-[transform,box-shadow] duration-[var(--motion-duration-normal)] ease-[var(--motion-ease-standard)] hover:-translate-y-0.5 hover:shadow-[var(--elevation-2)] motion-reduce:transition-none motion-reduce:hover:translate-y-0"
      style={
        {
          "--store-color": color,
          borderLeftColor: "var(--store-color)",
        } as CSSProperties
      }
    >
      <div className="flex items-center gap-3">
        <span
          className="v-store-badge"
          style={{ background: "var(--store-color)" }}
        >
          {badge}
        </span>
        <div className="min-w-0">
          <div className="type-ui font-mono font-medium">{label}</div>
          <div className="type-caption font-mono normal-case text-muted-foreground">
            {meta}
          </div>
        </div>
      </div>
      <p className="mt-3 type-ui text-muted-foreground">{desc}</p>
      <ul className="mt-3 flex flex-col gap-1.5">
        {items.length === 0 && emptyLabel ? (
          <li className="type-ui text-muted-foreground">{emptyLabel}</li>
        ) : (
          items.map((it) => (
            <li key={it} className="flex items-start gap-2">
              <span
                className="mt-1.5 size-1.5 shrink-0 rounded-full"
                style={{ background: "var(--store-color)" }}
                aria-hidden="true"
              />
              <span className={cn("type-ui", mono && "font-mono")}>{it}</span>
            </li>
          ))
        )}
      </ul>
    </div>
  );
}
