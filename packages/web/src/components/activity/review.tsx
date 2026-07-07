"use client";

import { AlertTriangle, ArrowRight } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useState } from "react";

import { useAuth } from "@/auth";
import { EmptyState } from "@/components/patterns/empty-state";
import { SkeletonBlock } from "@/components/patterns/loading";
import { kr } from "@/components/tasks/task-row";
import { Badge } from "@/components/ui/badge";
import {
  type DigestItem,
  type DigestSection,
  fetchReview,
  type MorningDigest,
} from "@/lib/api/review-client";
import { personaIdentityStyle } from "@/lib/persona-identity";

/** persona voice: an identity dot + the persona's name, then their own words (A6-D-2). */
function PersonaLine({
  item,
  name,
  voiced,
}: {
  item: DigestItem;
  name: string;
  voiced?: boolean;
}) {
  return (
    <div
      className="flex flex-col gap-0.5"
      style={personaIdentityStyle({ id: item.persona_id })}
    >
      <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
        <span
          aria-hidden="true"
          className="size-2 rounded-[2px]"
          style={{ background: "var(--v-id)" }}
        />
        {name}
      </span>
      <p
        className={
          voiced
            ? "type-body font-[family-name:var(--font-display)] italic"
            : "type-body"
        }
      >
        {item.title}
      </p>
      {item.detail ? (
        <p className="type-caption text-muted-foreground">{item.detail}</p>
      ) : null}
      {/* A7 provenance — renders "ran because …" once W8 wires it (null until then). */}
      {item.ran_because ? (
        <p className="type-caption text-muted-foreground">{item.ran_because}</p>
      ) : null}
    </div>
  );
}

/** Loud only where it informs: the stuck section is the one red-rail card (A6-D-5). */
function StuckItem({
  item,
  name,
  resolveLabel,
}: {
  item: DigestItem;
  name: string;
  resolveLabel: string;
}) {
  return (
    <div
      className="flex flex-col gap-2 rounded-md border border-destructive/30 border-l-2 border-l-destructive bg-destructive/5 p-3"
      style={personaIdentityStyle({ id: item.persona_id })}
      data-slot="review-stuck"
    >
      <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
        <AlertTriangle className="size-3.5 text-destructive" />
        {name}
      </span>
      <p className="type-body">{item.title}</p>
      {item.detail ? (
        <p className="type-caption text-destructive">{item.detail}</p>
      ) : null}
      {item.ran_because ? (
        <p className="type-caption text-muted-foreground">{item.ran_because}</p>
      ) : null}
      <Link
        href="/tasks"
        className="inline-flex w-fit items-center gap-1 text-sm text-foreground hover:underline"
      >
        {resolveLabel}
        <ArrowRight className="size-3.5" />
      </Link>
    </div>
  );
}

const SECTION_HREF: Record<string, string | null> = {
  waiting: "/approvals",
  stuck: null, // per-item resolve link
  done: null,
  initiatives: null,
};

export function Review() {
  const t = useTranslations("review");
  const { getToken } = useAuth();
  const [digest, setDigest] = useState<MorningDigest | null | "error">(null);

  const load = useCallback(async () => {
    try {
      setDigest(await fetchReview(await getToken()));
    } catch {
      setDigest("error");
    }
  }, [getToken]);

  useEffect(() => {
    void load();
  }, [load]);

  if (digest === null) {
    return (
      <div className="flex flex-col gap-4">
        <SkeletonBlock className="h-16" />
        <SkeletonBlock className="h-32" />
      </div>
    );
  }
  if (digest === "error") {
    return <p className="type-body text-muted-foreground">{t("loadFailed")}</p>;
  }

  const name = (id: string | null) =>
    id ? (digest.persona_names[id] ?? id) : "";
  const hasContent = digest.sections.length > 0 || digest.upcoming.length > 0;

  return (
    <div className="flex flex-col gap-8" data-slot="review">
      {/* the dateline: what the night cost, plainly */}
      <p className="type-caption text-muted-foreground">
        {digest.total_spent_micros > 0
          ? t("spentOvernight", { amount: kr(digest.total_spent_micros) })
          : t("quietSpend")}
      </p>

      {!hasContent ? (
        <EmptyState title={t("emptyTitle")} description={t("emptyBody")} />
      ) : null}

      {/* the spine: sections in the fixed priority order (waiting → stuck → done → initiatives) */}
      {digest.sections.map((section) => (
        <Section key={section.kind} section={section} name={name} t={t} />
      ))}

      {digest.upcoming.length > 0 ? (
        <section className="flex flex-col gap-2 border-l border-border pl-5">
          <h3 className="type-caption font-medium uppercase tracking-wide text-muted-foreground">
            {t("upcoming")}
          </h3>
          <ul className="flex flex-col gap-1">
            {digest.upcoming.map((u) => (
              <li
                key={`${u.fire_at}-${u.label}`}
                className="type-caption flex gap-2 text-muted-foreground"
              >
                <span>{u.label}</span>
                {u.persona_id ? <span>· {name(u.persona_id)}</span> : null}
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  );
}

function Section({
  section,
  name,
  t,
}: {
  section: DigestSection;
  name: (id: string | null) => string;
  t: ReturnType<typeof useTranslations>;
}) {
  const href = SECTION_HREF[section.kind];
  const isWaiting = section.kind === "waiting";
  const isDone = section.kind === "done";
  return (
    <section
      className="flex flex-col gap-3 border-l border-border pl-5"
      data-slot="review-section"
      data-kind={section.kind}
    >
      <div className="flex items-center gap-2">
        <h3 className="type-heading">{t(`section.${section.kind}`)}</h3>
        {isWaiting ? (
          <Badge
            variant="outline"
            className="text-amber-600 dark:text-amber-500"
          >
            {t("needsYou")}
          </Badge>
        ) : null}
      </div>

      {section.kind === "stuck" ? (
        section.items.map((item) => (
          <StuckItem
            key={`${item.persona_id}-${item.title}`}
            item={item}
            name={name(item.persona_id)}
            resolveLabel={t("resolve")}
          />
        ))
      ) : isDone ? (
        // done collapses to one-liners (A6-R-1 under-a-minute)
        <ul className="flex flex-col gap-1.5">
          {section.items.map((item) => (
            <li
              key={`${item.persona_id}-${item.title}`}
              className="type-body flex flex-wrap gap-x-2 text-muted-foreground"
            >
              <span className="text-foreground">{name(item.persona_id)}</span>
              <span>{item.title}</span>
              {item.detail ? <span>— {item.detail}</span> : null}
              {item.ran_because ? (
                <span className="text-muted-foreground/70">
                  {item.ran_because}
                </span>
              ) : null}
            </li>
          ))}
        </ul>
      ) : (
        <div className="flex flex-col gap-3">
          {section.items.map((item) => (
            <PersonaLine
              key={`${item.persona_id}-${item.title}`}
              item={item}
              name={name(item.persona_id)}
              voiced={isWaiting || section.kind === "initiatives"}
            />
          ))}
        </div>
      )}

      {section.overflow > 0 ? (
        <p className="type-caption text-muted-foreground">
          {t("overflow", { count: section.overflow })}
        </p>
      ) : null}

      {href ? (
        <Link
          href={href}
          className="inline-flex w-fit items-center gap-1 text-sm text-foreground hover:underline"
        >
          {t("reviewInApprovals")}
          <ArrowRight className="size-3.5" />
        </Link>
      ) : null}
    </section>
  );
}
