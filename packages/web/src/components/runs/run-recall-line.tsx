"use client";

import { useTranslations } from "next-intl";
import type { RunRecall } from "@/lib/run";

/**
 * part3 F10: the typed memory a run read before its first step, on the run header.
 *
 * The chat surface has named its recall since Spec 35 ("Recalling from <store> memory",
 * store coloured dot). A run had the same signal on the wire and threw it away, on the
 * surface where it matters most: a task that runs for days is where the persona's use of
 * its own memory is least obvious. This is the same vocabulary, past tense, because a run
 * header states what the run read rather than what it is reading right now. The dot is
 * `.v-recall-dot` for the store colours, held still (`data-static`) since nothing here is
 * in flight.
 */
const STORE_LABEL_KEYS: Record<string, string> = {
  identity: "recallStores.identity",
  self_facts: "recallStores.self_facts",
  worldview: "recallStores.worldview",
  episodic: "recallStores.episodic",
};

export function RunRecallLine({ recall }: { recall: RunRecall[] }) {
  const t = useTranslations("runs");
  if (recall.length === 0) return null;
  return (
    <div
      className="flex flex-wrap items-center gap-x-2 gap-y-1"
      title={t("recallTitle")}
      data-slot="run-view-recall"
    >
      <span className="type-caption font-mono text-muted-foreground uppercase">
        {t("recallLabel")}
      </span>
      {recall.map((entry) => {
        const key = STORE_LABEL_KEYS[entry.store];
        // An unknown store token prints itself rather than a missing-key crash: the
        // record is data, and a fifth store would ship before this catalogue knew it.
        const store = key ? t(key) : entry.store;
        return (
          <span
            key={entry.store}
            className="flex items-center gap-1.5"
            data-slot="run-view-recall-store"
            data-store={entry.store}
          >
            <span
              className="v-recall-dot"
              data-store={entry.store}
              data-static="true"
              aria-hidden="true"
            />
            <span className="type-caption font-mono text-muted-foreground">
              {entry.count === undefined
                ? store
                : t("recallStore", { store, count: entry.count })}
            </span>
          </span>
        );
      })}
    </div>
  );
}
