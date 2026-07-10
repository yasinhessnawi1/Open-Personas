import { ArrowRight, Sparkles } from "lucide-react";
import Link from "next/link";
import { redirect } from "next/navigation";
import { getTranslations } from "next-intl/server";
import { auth } from "@/auth/server";
import { InstrumentStats } from "@/components/home/instrument-stats";
import { Onboarding } from "@/components/home/onboarding";
import {
  QuickLaunchItem,
  type QuickLaunchPersona,
} from "@/components/home/quick-launch-item";
import {
  type RecentConversationItem,
  RecentConversations,
} from "@/components/home/recent-conversations";
import {
  Grid,
  PageBody,
  PageHeader,
  Section,
  Stack,
} from "@/components/layout";
import { AppShell } from "@/components/shell/app-shell";
import { buttonVariants } from "@/components/ui/button";
import { unwrap } from "@/lib/api";
import { serverApi } from "@/lib/api/server";
import { cn } from "@/lib/utils";

/** How many fast-launch personas + resume rows the dashboard surfaces. */
const TOP_PERSONAS = 4;
const RECENT_CONVERSATIONS = 5;

/**
 * Auth-aware product root (`/`).
 *
 *   - signed-out → redirect to the standalone marketing site
 *     (NEXT_PUBLIC_MARKETING_URL); falls back to the in-app /sign-in route when
 *     unset so the app stays usable standalone.
 *   - signed-in + NO personas (new user) → the <Onboarding/> empty state.
 *   - signed-in + HAS personas → a lightweight FAST-LAUNCH dashboard (get back
 *     to a persona and chat/call fast). This is visually + functionally
 *     distinct from `/personas` (the full management grid, untouched): it shows
 *     only the top-few most-recently-used personas as compact launchers plus
 *     recent conversations to resume — NOT a management grid.
 *
 * `/` lives outside the (app) route group (so it can redirect signed-out users
 * before the shell mounts), so the signed-in surfaces wrap themselves in
 * <AppShell> to match every other authenticated screen.
 *
 * RECENCY: derived entirely from existing data. `/v1/conversations` returns
 * conversations already sorted `updated_at DESC` server-side; we group by
 * persona to rank "most recently used" personas and slice the head for "resume"
 * rows. Personas never yet talked to fall back to most-recently-created
 * (PersonaSummary.created_at). No favorites/pin field, no schema/DB migration.
 */
export default async function RootPage() {
  const { userId } = await auth();

  if (!userId) {
    const marketingUrl = process.env.NEXT_PUBLIC_MARKETING_URL?.trim();
    redirect(
      marketingUrl && marketingUrl.length > 0 ? marketingUrl : "/sign-in",
    );
  }

  const t = await getTranslations("home");
  const api = await serverApi();
  // Spec 35 D-35-1: the dashboard instrument stats are wired to real data.
  // Credits is fetched DEFENSIVELY (`.data`, not `unwrap`) so a credits-fetch
  // failure (e.g. the 402 exhausted cliff) degrades the stat tile rather than
  // breaking the whole dashboard.
  const [personas, conversations, creditsRes] = await Promise.all([
    api.GET("/v1/personas").then(unwrap),
    api
      .GET("/v1/conversations", {
        params: { query: { limit: 50, offset: 0 } },
      })
      .then(unwrap),
    api.GET("/v1/me/credits"),
  ]);
  const credits = creditsRes.data?.balance ?? null;
  const edition =
    process.env.PERSONA_EDITION === "cloud" ? "cloud" : "community";

  if (personas.length === 0) {
    return (
      <AppShell>
        <PageBody>
          <PageHeader title={t("title")} subtitle={t("subtitleNew")} />
          <Onboarding />
        </PageBody>
      </AppShell>
    );
  }

  const personaById = new Map(personas.map((p) => [p.id, p]));

  // Rank personas by most-recent use: conversations are already updated_at-DESC,
  // so first appearance of each persona_id is its most-recent activity.
  const usedOrder: (typeof personas)[number][] = [];
  const seen = new Set<string>();
  for (const c of conversations) {
    const p = personaById.get(c.persona_id);
    if (p && !seen.has(p.id)) {
      seen.add(p.id);
      usedOrder.push(p);
    }
  }
  // Fill the tail with not-yet-used personas, most-recently-created first.
  const unusedByCreated = personas
    .filter((p) => !seen.has(p.id))
    .sort((a, b) => b.created_at.localeCompare(a.created_at));

  const topPersonas: QuickLaunchPersona[] = [...usedOrder, ...unusedByCreated]
    .slice(0, TOP_PERSONAS)
    .map((p) => ({
      id: p.id,
      name: p.name,
      role: p.role,
      avatar_url: p.avatar_url,
    }));

  const recent: RecentConversationItem[] = conversations
    .slice(0, RECENT_CONVERSATIONS)
    .map((c) => {
      const p = personaById.get(c.persona_id);
      return {
        id: c.id,
        title: c.title,
        updated_at: c.updated_at,
        persona: p
          ? { id: p.id, name: p.name, avatar_url: p.avatar_url }
          : null,
      };
    });

  return (
    <AppShell>
      <PageBody>
        <PageHeader
          title={t("title")}
          subtitle={t("subtitle")}
          actions={
            <Link
              href="/personas/new"
              className={cn(buttonVariants(), "gap-2")}
            >
              <Sparkles className="size-4" aria-hidden="true" />
              {t("create")}
            </Link>
          }
        />

        <Stack gap={8}>
          <InstrumentStats
            credits={credits}
            personaCount={personas.length}
            conversationCount={conversations.length}
            edition={edition}
          />

          <Section heading={t("jumpBackIn")}>
            <Grid cols={{ base: 1, lg: 2 }} gap={4}>
              {topPersonas.map((p) => (
                <QuickLaunchItem key={p.id} persona={p} />
              ))}
            </Grid>
          </Section>

          {recent.length > 0 ? (
            <Section heading={t("recent.heading")}>
              <RecentConversations conversations={recent} />
            </Section>
          ) : null}

          <div className="flex flex-wrap items-center gap-3">
            <Link
              href="/personas"
              className={cn(buttonVariants({ variant: "outline" }), "gap-2")}
            >
              {t("manageAll")}
              <ArrowRight className="size-4" aria-hidden="true" />
            </Link>
            <Link
              href="/personas/new"
              className={cn(buttonVariants({ variant: "ghost" }), "gap-2")}
            >
              <Sparkles className="size-4" aria-hidden="true" />
              {t("create")}
            </Link>
          </div>
        </Stack>
      </PageBody>
    </AppShell>
  );
}
