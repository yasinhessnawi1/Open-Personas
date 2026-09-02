"use client";

import { AlertTriangle, ArrowRight } from "lucide-react";
import Link from "next/link";
import { useLocale, useTranslations } from "next-intl";
import { useCallback, useEffect, useState } from "react";

import { useAuth } from "@/auth";
import {
  type NewTaskAction,
  NewTaskDialog,
  type NewTaskPersona,
} from "@/components/activity/new-task-dialog";
import { ReviewDateline } from "@/components/activity/review-dateline";
import { EmptyState } from "@/components/patterns/empty-state";
import { SkeletonBlock } from "@/components/patterns/loading";
import { buttonVariants } from "@/components/ui/button";
import {
  type DigestItem,
  type DigestRef,
  type DigestSection,
  fetchReview,
  type MorningDigest,
} from "@/lib/api/review-client";
import { personaIdentityStyle } from "@/lib/persona-identity";
import { useTaskSignal } from "@/lib/task-signal";
import { cn } from "@/lib/utils";

/**
 * Spec A6 (W5) → R11-B2: the morning Review in the ratified A6-R-1 register —
 * Dateline spine with the LEDGER's triage affordances. State is encoded in
 * FORM, not just words: every actionable line is a card with a 3px state rail
 * (amber = waiting, red = stuck — the one loud moment, green = the done
 * group), a tinted pill, and an inline action. Persona identity is a swatch
 * with its tint ring + name — never a surface wash. Done compresses to dense
 * rows in ONE quiet card; Upcoming is a horizontal strip in the muted
 * register. Fraunces carries section heads and the personas' own words
 * (voiced lines italic).
 */

/** The per-item deep-link target (A6-D-6): approval → the inbox item, task → the task detail.
 * R11-B1: targets live under the consolidated /activity area (D-R11-4). */
function refHref(ref: DigestRef | null): string | null {
  if (!ref) return null;
  if (ref.kind === "approval")
    return `/activity/approvals?id=${encodeURIComponent(ref.id)}`;
  return `/activity/tasks/${encodeURIComponent(ref.id)}`;
}

/** Identity swatch: the persona's derived colour as a small square with a soft
 * tint ring (the artifact's `.swatch`) — reads as a person, not decoration. */
function Swatch() {
  return (
    <span
      aria-hidden="true"
      className="size-2.5 shrink-0 rounded-[3px]"
      style={{
        background: "var(--v-id)",
        boxShadow:
          "0 0 0 3px color-mix(in oklch, var(--v-id) 18%, transparent)",
      }}
    />
  );
}

type PillTone = "attention" | "critical" | "good" | "scheduled";

const PILL_TONES: Record<PillTone, string> = {
  attention: "bg-amber-500/15 text-amber-700 dark:text-amber-400",
  critical: "bg-destructive/10 text-destructive",
  good: "bg-emerald-500/15 text-emerald-700 dark:text-emerald-400",
  scheduled: "bg-sky-500/15 text-sky-700 dark:text-sky-400",
};

/** Tinted state pill — loud carries information, never decoration. */
function Pill({
  tone,
  children,
}: {
  tone: PillTone;
  children: React.ReactNode;
}) {
  return (
    <span
      className={cn(
        "rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.03em] leading-relaxed",
        PILL_TONES[tone],
      )}
    >
      {children}
    </span>
  );
}

const RAIL_TONES: Record<PillTone, string> = {
  attention: "bg-amber-500/80",
  critical: "bg-destructive",
  good: "bg-emerald-500/70",
  scheduled: "bg-sky-500/60",
};

/** A card with the artifact's 3px state rail. */
function RailCard({
  tone,
  className,
  children,
  ...rest
}: {
  tone?: PillTone;
  className?: string;
  children: React.ReactNode;
} & React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn(
        "relative overflow-hidden rounded-lg border border-border bg-card p-4 shadow-xs",
        tone && "pl-5",
        tone === "critical" && "border-destructive/40",
        className,
      )}
      {...rest}
    >
      {tone ? (
        <span
          aria-hidden="true"
          className={cn("absolute inset-y-0 left-0 w-[3px]", RAIL_TONES[tone])}
        />
      ) : null}
      {children}
    </div>
  );
}

/** Fraunces section head with an honest count chip + hairline rule (artifact `.sec-head`). */
function SectionHead({ title, count }: { title: string; count: number }) {
  return (
    <div className="flex items-baseline gap-2.5">
      <h3 className="font-heading text-lg font-medium tracking-tight">
        {title}
      </h3>
      <span className="rounded-full border border-border/60 bg-muted px-1.5 py-px text-[11px] tabular-nums text-muted-foreground">
        {count}
      </span>
      <span
        aria-hidden="true"
        className="h-px flex-1 self-center bg-border/60"
      />
    </div>
  );
}

function ItemWho({ name }: { name: string }) {
  return (
    <span className="flex items-center gap-2 text-sm font-medium">
      <Swatch />
      {name}
    </span>
  );
}

function ItemAction({
  ref_,
  label,
  primary = false,
}: {
  ref_: DigestRef | null;
  label: string;
  primary?: boolean;
}) {
  const href = refHref(ref_);
  if (!href) return null;
  return (
    <Link
      href={href}
      className={cn(
        buttonVariants({
          variant: primary ? "default" : "outline",
          size: "sm",
        }),
        "w-fit gap-1.5",
      )}
    >
      {label}
      <ArrowRight className="size-3.5" aria-hidden="true" />
    </Link>
  );
}

/** Waiting on you: an attention-rail card — the persona's own words (Fraunces
 * italic), the detail in the mono register, the action inline (Ledger triage). */
function WaitingCard({
  item,
  name,
  t,
}: {
  item: DigestItem;
  name: string;
  t: ReturnType<typeof useTranslations>;
}) {
  return (
    <RailCard
      tone="attention"
      style={personaIdentityStyle({ id: item.persona_id })}
    >
      <div className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <ItemWho name={name} />
          <Pill tone="attention">{t("needsYou")}</Pill>
        </div>
        <p className="font-heading text-[0.95rem] italic leading-relaxed">
          {item.title}
        </p>
        {item.detail ? (
          <p className="type-caption normal-case tracking-normal text-muted-foreground">
            {item.detail}
          </p>
        ) : null}
        {item.ran_because ? (
          <p className="type-caption text-muted-foreground">
            {item.ran_because}
          </p>
        ) : null}
        <ItemAction
          ref_={item.ref}
          label={
            item.ref?.kind === "approval"
              ? t("reviewInApprovals")
              : t("resolve")
          }
          primary
        />
      </div>
    </RailCard>
  );
}

/** Loud only where it informs: stuck is the ONE red-rail card (A6-D-5). */
function StuckCard({
  item,
  name,
  t,
}: {
  item: DigestItem;
  name: string;
  t: ReturnType<typeof useTranslations>;
}) {
  return (
    <RailCard
      tone="critical"
      style={personaIdentityStyle({ id: item.persona_id })}
      data-slot="review-stuck"
    >
      <div className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <ItemWho name={name} />
          <Pill tone="critical">
            <AlertTriangle
              className="mr-1 inline size-3 -translate-y-px"
              aria-hidden="true"
            />
            {t("section.stuck")}
          </Pill>
        </div>
        <p className="type-body">{item.title}</p>
        {item.detail ? (
          <p className="type-caption normal-case tracking-normal text-destructive">
            {item.detail}
          </p>
        ) : null}
        {item.ran_because ? (
          <p className="type-caption text-muted-foreground">
            {item.ran_because}
          </p>
        ) : null}
        <ItemAction ref_={item.ref} label={t("resolve")} primary />
      </div>
    </RailCard>
  );
}

/** Done overnight: ONE quiet green-rail card, dense letter-register rows. */
function DoneCard({
  items,
  name,
}: {
  items: DigestItem[];
  name: (id: string | null) => string;
}) {
  return (
    <RailCard tone="good" className="py-2">
      <ul className="divide-y divide-border/60">
        {items.map((item, i) => (
          <li
            key={`${i}-${item.persona_id}`}
            className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5 py-2.5"
            style={personaIdentityStyle({ id: item.persona_id })}
          >
            <span className="flex min-w-24 items-center gap-2 text-sm font-medium">
              <Swatch />
              {name(item.persona_id)}
            </span>
            <span className="flex-1 text-sm text-muted-foreground">
              {refHref(item.ref) ? (
                <Link
                  href={refHref(item.ref) as string}
                  className="text-foreground hover:underline"
                >
                  {item.title}
                </Link>
              ) : (
                <span className="text-foreground">{item.title}</span>
              )}
              {item.detail ? <> · {item.detail}</> : null}
              {item.ran_because ? (
                <span className="text-muted-foreground/70">
                  {" "}
                  {item.ran_because}
                </span>
              ) : null}
            </span>
          </li>
        ))}
      </ul>
    </RailCard>
  );
}

/** Noticed (initiatives): a calm card — the persona's suggestion in their own
 * voice, a scheduled-tone pill, the action ghosted. */
function IdeaCard({
  item,
  name,
  t,
}: {
  item: DigestItem;
  name: string;
  t: ReturnType<typeof useTranslations>;
}) {
  return (
    <RailCard style={personaIdentityStyle({ id: item.persona_id })}>
      <div className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <ItemWho name={name} />
          <Pill tone="scheduled">{t("ideaPill")}</Pill>
        </div>
        <p className="border-l-2 border-border pl-3 font-heading text-[0.95rem] italic leading-relaxed text-foreground/90">
          {item.title}
        </p>
        {item.detail ? (
          <p className="type-caption normal-case tracking-normal text-muted-foreground">
            {item.detail}
          </p>
        ) : null}
        {item.ran_because ? (
          <p className="type-caption text-muted-foreground">
            {item.ran_because}
          </p>
        ) : null}
        <ItemAction ref_={item.ref} label={t("resolve")} />
      </div>
    </RailCard>
  );
}

export function Review({
  personas = [],
  newTaskAction,
}: {
  /** Executor options for the header's New-task dialog (server-fetched). */
  personas?: readonly NewTaskPersona[];
  /** The dispatch server action, threaded from the page (R11-B2). */
  newTaskAction?: NewTaskAction;
}) {
  const t = useTranslations("review");
  const locale = useLocale();
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

  // W8: on a task.updated signal, refetch the durable digest — never trust a pushed state (A6-R-4).
  useTaskSignal(() => void load());

  if (digest === null) {
    return (
      <div className="flex flex-col gap-4">
        <SkeletonBlock className="h-20" />
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
    <div className="flex flex-col gap-7" data-slot="review">
      {/* R11-B2: the A6-R-1 dateline header — the surface's h1 — with the kit's
          persistent New-task affordance beside it. */}
      <div className="flex flex-wrap items-end justify-between gap-4">
        <ReviewDateline digest={digest} />
        {personas.length > 0 && newTaskAction ? (
          <NewTaskDialog personas={personas} action={newTaskAction} />
        ) : null}
      </div>

      {!hasContent ? (
        <EmptyState title={t("emptyTitle")} description={t("emptyBody")} />
      ) : null}

      {/* the spine: sections in the fixed priority order (waiting → stuck → done → initiatives) */}
      {digest.sections.map((section) => (
        <Section key={section.kind} section={section} name={name} t={t} />
      ))}

      {digest.upcoming.length > 0 ? (
        <section
          className="flex flex-col gap-3 rounded-lg border border-border/70 bg-muted/40 p-4"
          data-slot="review-upcoming"
        >
          <h3 className="type-caption font-medium uppercase tracking-wide text-muted-foreground">
            ↑ {t("upcoming")}
          </h3>
          <div className="flex gap-2 overflow-x-auto pb-1">
            {digest.upcoming.map((u, i) => (
              <div
                key={`${i}-${u.fire_at}`}
                className="min-w-36 shrink-0 rounded-md border border-border bg-card px-3 py-2"
                style={
                  u.persona_id
                    ? personaIdentityStyle({ id: u.persona_id })
                    : undefined
                }
              >
                <p className="type-caption normal-case tracking-normal tabular-nums text-muted-foreground">
                  {new Date(u.fire_at).toLocaleString(locale, {
                    weekday: "short",
                    hour: "2-digit",
                    minute: "2-digit",
                    hour12: false,
                  })}
                </p>
                <p className="mt-0.5 flex items-center gap-1.5 text-[13px] font-medium">
                  {u.persona_id ? <Swatch /> : null}
                  <span className="truncate">{u.label}</span>
                </p>
              </div>
            ))}
          </div>
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
  return (
    <section
      className="flex flex-col gap-3"
      data-slot="review-section"
      data-kind={section.kind}
    >
      <SectionHead
        title={t(`section.${section.kind}`)}
        count={section.items.length + section.overflow}
      />

      {section.kind === "waiting" ? (
        section.items.map((item, i) => (
          <WaitingCard
            key={`${i}-${item.persona_id}`}
            item={item}
            name={name(item.persona_id)}
            t={t}
          />
        ))
      ) : section.kind === "stuck" ? (
        section.items.map((item, i) => (
          <StuckCard
            key={`${i}-${item.persona_id}`}
            item={item}
            name={name(item.persona_id)}
            t={t}
          />
        ))
      ) : section.kind === "done" ? (
        <DoneCard items={section.items} name={name} />
      ) : (
        section.items.map((item, i) => (
          <IdeaCard
            key={`${i}-${item.persona_id}`}
            item={item}
            name={name(item.persona_id)}
            t={t}
          />
        ))
      )}

      {section.overflow > 0 ? (
        <p className="type-caption text-muted-foreground">
          {t("overflow", { count: section.overflow })}
        </p>
      ) : null}
    </section>
  );
}
