import { Package } from "lucide-react";
import { getFormatter, getTranslations } from "next-intl/server";
import { currentUser } from "@/auth/server";
import { PageBody, PageHeader, Stack } from "@/components/layout";
import { ErrorState } from "@/components/patterns/error-state";
import { LowBalanceWarningCard } from "@/components/settings/low-balance-warning-card";
import { PreferencesCard } from "@/components/settings/preferences-card";
import { Card } from "@/components/ui/card";
import { unwrap } from "@/lib/api";
import { serverApi } from "@/lib/api/server";

/**
 * Spec F2 T31 — Settings page (rebuilt presentation).
 *
 * DO NOT TOUCH (per audit.md §settings.plumbing):
 *   - `currentUser()` from Clerk + `serverApi()` GET `/v1/me/credits` + GET
 *     `/v1/me/usage` (parallel fetch);
 *   - `credits.balance` (D-11-12 zero-guard surfaces via the `exhausted`
 *     branch below);
 *   - `<PreferencesCard>` consumer of `useTheme` + `useBoolSetting` + `LOCALE_COOKIE`.
 *
 * Low-balance (D-11-12): `credits.low_balance` is surfaced inline via
 * `<LowBalanceWarningCard>` above the credits card when the backend flips the
 * flag and balance > 0. The credits-exhausted (balance === 0) cliff stays on
 * the existing T22 `<ErrorState status={402}>` branch below.
 *
 * REPLACED:
 *   - hand-rolled `mx-auto max-w-3xl` → T20 `<PageBody>`;
 *   - hand-rolled h1 `font-heading text-3xl` → T20 `<PageHeader>`;
 *   - section h2 `font-heading text-sm tracking-wide uppercase` →
 *     `.type-caption font-mono uppercase` (consistent with the F2 byline
 *     pattern seen in run viewer + authoring);
 *   - body `text-sm` / `text-xs` → `.type-body` / `.type-ui` / `.type-caption`;
 *   - `text-3xl` credit balance → `.type-display` (the Fraunces hero scale);
 *   - credits-exhausted (balance === 0) now surfaces via T22 `<ErrorState
 *     status={402}>` with `creditsExhausted` + `creditsExhaustedHint` copy.
 */
export default async function SettingsPage() {
  const t = await getTranslations("settings");
  // Locale-pinned both sides (server variant — this is a Server Component) —
  // see conversation-list.tsx / R9-044.
  const format = await getFormatter();
  const api = await serverApi();
  const [user, credits, usage] = await Promise.all([
    currentUser(),
    api.GET("/v1/me/credits").then(unwrap),
    api.GET("/v1/me/usage").then(unwrap),
  ]);

  const email = user?.primaryEmailAddress?.emailAddress ?? "";
  const name =
    [user?.firstName, user?.lastName].filter(Boolean).join(" ") || email;
  const exhausted = credits.balance === 0;

  return (
    <PageBody>
      <PageHeader title={t("title")} subtitle={t("subtitle")} />

      <div className="lg:grid lg:grid-cols-[14rem_1fr] lg:gap-6">
        <nav
          aria-label={t("sectionsNav")}
          className="sticky top-20 hidden self-start lg:block"
          data-slot="settings-anchor-nav"
        >
          <ul className="flex flex-col gap-1 border-l text-muted-foreground">
            <li>
              <a
                href="#profile"
                className="type-ui block border-l-2 border-transparent px-3 py-1 hover:border-primary hover:text-foreground"
              >
                {t("profileLabel")}
              </a>
            </li>
            <li>
              <a
                href="#credits"
                className="type-ui block border-l-2 border-transparent px-3 py-1 hover:border-primary hover:text-foreground"
              >
                {t("credits")}
              </a>
            </li>
            <li>
              <a
                href="#preferences"
                className="type-ui block border-l-2 border-transparent px-3 py-1 hover:border-primary hover:text-foreground"
              >
                {t("preferences")}
              </a>
            </li>
            <li>
              <a
                href="#usage"
                className="type-ui block border-l-2 border-transparent px-3 py-1 hover:border-primary hover:text-foreground"
              >
                {t("usage")}
              </a>
            </li>
            <li>
              <a
                href="#about"
                className="type-ui block border-l-2 border-transparent px-3 py-1 hover:border-primary hover:text-foreground"
              >
                {t("aboutLabel")}
              </a>
            </li>
          </ul>
        </nav>

        <Stack gap={5}>
          <Card
            id="profile"
            className="gap-2 p-5 scroll-mt-20"
            data-slot="settings-account"
          >
            <h2 className="type-caption font-mono text-muted-foreground uppercase">
              {t("account")}
            </h2>
            <p className="type-body font-medium">{name}</p>
            {email && name !== email ? (
              <p className="type-ui text-muted-foreground">{email}</p>
            ) : null}
            <p className="type-caption text-muted-foreground">
              {t("accountHint")}
            </p>
          </Card>

          <LowBalanceWarningCard
            credits={credits}
            title={t("lowBalance")}
            hint={t("creditsHint")}
          />

          {exhausted ? (
            <ErrorState
              status={402}
              copy={{
                title: t("creditsExhausted"),
                description: t("creditsExhaustedHint"),
              }}
            />
          ) : (
            <Card
              id="credits"
              className="gap-2 p-5 scroll-mt-20"
              data-slot="settings-credits"
            >
              <h2 className="type-caption font-mono text-muted-foreground uppercase">
                {t("credits")}
              </h2>
              <p
                className="type-display tabular-nums"
                data-slot="settings-credits-balance"
              >
                {format.number(credits.balance)}
              </p>
              <p className="type-caption text-muted-foreground">
                {t("creditsHint")}
              </p>
            </Card>
          )}

          <div id="preferences" className="scroll-mt-20">
            <PreferencesCard />
          </div>

          <Card
            id="usage"
            className="gap-3 p-5 scroll-mt-20"
            data-slot="settings-usage"
          >
            <h2 className="type-caption font-mono text-muted-foreground uppercase">
              {t("usage")}
            </h2>
            {usage.length === 0 ? (
              <p className="type-ui text-muted-foreground">{t("usageEmpty")}</p>
            ) : (
              <div className="overflow-x-auto">
                <table className="type-ui w-full">
                  <thead>
                    <tr className="type-caption border-b text-left text-muted-foreground uppercase">
                      <th className="py-2 pr-3 font-medium">{t("colWhen")}</th>
                      <th className="py-2 pr-3 font-medium">{t("colTier")}</th>
                      <th className="py-2 pr-3 font-medium">{t("colModel")}</th>
                      <th className="py-2 pr-3 text-right font-medium">
                        {t("colTokens")}
                      </th>
                      <th className="py-2 text-right font-medium">
                        {t("colCost")}
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {usage.map((row, i) => (
                      <tr
                        // biome-ignore lint/suspicious/noArrayIndexKey: usage rows have no id
                        key={i}
                        className="border-b last:border-0"
                      >
                        <td className="py-2 pr-3 whitespace-nowrap text-muted-foreground">
                          {format.dateTime(new Date(row.created_at), {
                            dateStyle: "medium",
                            timeStyle: "short",
                          })}
                        </td>
                        <td className="py-2 pr-3">
                          <span className="type-caption font-mono uppercase">
                            {row.tier_used}
                          </span>
                        </td>
                        <td className="type-caption py-2 pr-3 font-mono">
                          {row.model_name}
                        </td>
                        <td className="py-2 pr-3 text-right tabular-nums">
                          {format.number(
                            row.prompt_tokens + row.completion_tokens,
                          )}
                        </td>
                        <td className="py-2 text-right tabular-nums">
                          ${(row.cost_cents / 100).toFixed(4)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>

          <Card
            id="about"
            className="gap-3 p-5 scroll-mt-20"
            data-slot="settings-about"
          >
            <h2 className="type-caption font-mono text-muted-foreground uppercase">
              {t("aboutLabel")}
            </h2>
            <dl className="type-body grid grid-cols-[auto_1fr] gap-x-4 gap-y-1">
              <dt className="text-muted-foreground">{t("aboutVersion")}</dt>
              <dd className="font-mono">persona-web 0.13.0+f5</dd>
            </dl>
            <div
              className="mt-1 flex flex-wrap gap-2"
              data-slot="settings-links"
            >
              <AboutLink
                href="https://github.com/yasinhessnawi1/Open-Persona"
                label={t("github")}
              >
                <GithubMark className="size-3.5" />
              </AboutLink>
              <AboutLink
                href="https://pypi.org/project/persona-core/"
                label="persona-core"
              >
                <Package className="size-3.5" aria-hidden />
              </AboutLink>
              <AboutLink
                href="https://pypi.org/project/persona-runtime/"
                label="persona-runtime"
              >
                <Package className="size-3.5" aria-hidden />
              </AboutLink>
              <AboutLink
                href="https://pypi.org/project/persona-voice/"
                label="persona-voice"
              >
                <Package className="size-3.5" aria-hidden />
              </AboutLink>
            </div>
          </Card>
        </Stack>
      </div>
    </PageBody>
  );
}

/** An external source/package link — icon + label chip (opens in a new tab). */
function AboutLink({
  href,
  label,
  children,
}: {
  href: string;
  label: string;
  children: React.ReactNode;
}) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="type-caption inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
    >
      {children}
      {label}
    </a>
  );
}

/** GitHub mark — lucide dropped its brand icons, so this is the canonical mark. */
function GithubMark({ className }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="currentColor"
      aria-hidden="true"
      className={className}
    >
      <path d="M12 .297c-6.63 0-12 5.373-12 12 0 5.303 3.438 9.8 8.205 11.385.6.113.82-.258.82-.577 0-.285-.01-1.04-.015-2.04-3.338.724-4.042-1.61-4.042-1.61C4.422 18.07 3.633 17.7 3.633 17.7c-1.087-.744.084-.729.084-.729 1.205.084 1.838 1.236 1.838 1.236 1.07 1.835 2.809 1.305 3.495.998.108-.776.417-1.305.76-1.605-2.665-.3-5.466-1.332-5.466-5.93 0-1.31.465-2.38 1.235-3.22-.135-.303-.54-1.523.105-3.176 0 0 1.005-.322 3.3 1.23.96-.267 1.98-.399 3-.405 1.02.006 2.04.138 3 .405 2.28-1.552 3.285-1.23 3.285-1.23.645 1.653.24 2.873.12 3.176.765.84 1.23 1.91 1.23 3.22 0 4.61-2.805 5.625-5.475 5.92.42.36.81 1.096.81 2.22 0 1.606-.015 2.896-.015 3.286 0 .315.21.69.825.57C20.565 22.092 24 17.592 24 12.297c0-6.627-5.373-12-12-12" />
    </svg>
  );
}
