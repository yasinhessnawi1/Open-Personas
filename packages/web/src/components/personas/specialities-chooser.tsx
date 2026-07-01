"use client";
import { Check, GraduationCap, ShieldCheck } from "lucide-react";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useAuth } from "@/auth";
import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { Input } from "@/components/ui/input";
import {
  fetchSpecialities,
  recordSpecialityConsent,
  type SpecialityEntry,
} from "@/lib/specialities/specialities";
import { cn } from "@/lib/utils";
import {
  deriveSpecialityState,
  isSpecialityEnabled,
  needsConsent,
  type SpecialityState,
} from "./speciality-state";

const TEMPLATE = process.env.NEXT_PUBLIC_CLERK_JWT_TEMPLATE;

/**
 * Spec S3 — the Specialities chooser (the S-track's human-facing half).
 *
 * Surfaces skills as installable "Specialities": a searchable directory of cards,
 * each expanding to a detail view that holds the trust tier, the guidance the
 * speciality brings (`when_to_use`/`not_for`), and — INSIDE the expanded detail,
 * after the disclosure — the enable control (see-then-grant, N3's locked
 * expand-to-enable). Semantically DISTINCT from the apps chooser: a knowledge glyph,
 * an "expertise your persona will follow" framing, a trust-TIER badge (not signed/
 * risk), and guidance fields an app lacks.
 *
 * Enablement = the `skills:` declaration (drives `onChange`, the existing YAML path).
 * Consent = a separate server-side record: a community/third_party speciality shows an
 * honest disclosure and requires an explicit grant before it can enable (S1-D-4);
 * declined → not enabled. Consent binds to the body hash server-side, so a changed
 * body re-gates (S1-D-5): a `stale`/`none` gated declaration surfaces as needs-consent.
 * **Friction sits on enable only** — disabling is one click, no ceremony.
 */
export function SpecialitiesChooser({
  personaId,
  declaredSkills,
  onChange,
  initialItems,
}: {
  /** The persona being edited; absent in the author/new flow (no persona to consent against yet). */
  personaId?: string;
  declaredSkills: string[];
  onChange: (skills: string[]) => void;
  /**
   * Pre-seeded catalog — when provided, the client fetch is skipped (a preview/
   * reference-render + SSR seam; production leaves it undefined and self-fetches).
   */
  initialItems?: SpecialityEntry[];
}) {
  const t = useTranslations("specialities");
  const { getToken } = useAuth();
  const [items, setItems] = useState<SpecialityEntry[] | null>(
    initialItems ?? null,
  );
  const [loadError, setLoadError] = useState(false);
  const [query, setQuery] = useState("");

  const tokenGetter = useCallback(
    () => getToken(TEMPLATE ? { template: TEMPLATE } : undefined),
    [getToken],
  );

  useEffect(() => {
    if (initialItems) return; // seeded (preview/SSR) — no client fetch.
    let live = true;
    fetchSpecialities(personaId, tokenGetter)
      .then((list) => live && setItems(list))
      .catch(() => live && setLoadError(true));
    return () => {
      live = false;
    };
  }, [personaId, tokenGetter, initialItems]);

  // Declared-but-dropped: a speciality the persona still lists but the catalog no
  // longer carries → a graceful tombstone (never a broken card).
  const unavailable = useMemo(() => {
    if (!items) return [];
    const known = new Set(items.map((s) => s.name));
    return declaredSkills.filter((name) => !known.has(name));
  }, [items, declaredSkills]);

  const filtered = useMemo(() => {
    if (!items) return [];
    const q = query.trim().toLowerCase();
    if (!q) return items;
    return items.filter((s) =>
      `${s.name} ${s.description} ${s.source ?? ""}`.toLowerCase().includes(q),
    );
  }, [items, query]);

  function setConsentState(name: string, updated: SpecialityEntry) {
    setItems(
      (prev) => prev?.map((s) => (s.name === name ? updated : s)) ?? prev,
    );
  }

  function enable(name: string) {
    if (!isSpecialityEnabled(name, declaredSkills))
      onChange([...declaredSkills, name]);
  }
  function disable(name: string) {
    onChange(declaredSkills.filter((n) => n !== name));
  }

  if (loadError) {
    return (
      <p className="text-sm text-destructive" data-slot="specialities-error">
        {t("loadError")}
      </p>
    );
  }
  if (items === null) {
    return (
      <p
        className="text-sm text-muted-foreground"
        data-slot="specialities-loading"
      >
        {t("loading")}
      </p>
    );
  }
  if (items.length === 0 && unavailable.length === 0) {
    return <p className="text-sm text-muted-foreground">{t("empty")}</p>;
  }

  return (
    <div className="flex flex-col gap-3" data-slot="specialities-chooser">
      <Input
        type="search"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder={t("searchPlaceholder")}
        aria-label={t("searchPlaceholder")}
        data-slot="specialities-search"
      />
      {filtered.length === 0 && query.trim() ? (
        <p
          className="text-sm text-muted-foreground"
          data-slot="specialities-search-empty"
        >
          {t("searchEmpty", { query: query.trim() })}
        </p>
      ) : (
        <ul className="flex flex-col gap-2">
          {filtered.map((s) => (
            <li key={s.name}>
              <SpecialityCard
                entry={s}
                state={deriveSpecialityState(s, declaredSkills, unavailable)}
                personaId={personaId}
                onEnable={() => enable(s.name)}
                onDisable={() => disable(s.name)}
                onConsented={(updated) => {
                  setConsentState(s.name, updated);
                  enable(s.name);
                }}
                tokenGetter={tokenGetter}
              />
            </li>
          ))}
        </ul>
      )}
      {unavailable.map((name) => (
        <UnavailableSpeciality
          key={name}
          name={name}
          onRemove={() => disable(name)}
        />
      ))}
    </div>
  );
}

/** The tier chip — positive-elevation: earn "Vetted"; calm-honest for the lower tiers. */
function TierBadge({ entry }: { entry: SpecialityEntry }) {
  const t = useTranslations("specialities");
  if (entry.trust === "vetted") {
    return (
      <Badge variant="default" data-slot="speciality-tier" data-tier="vetted">
        <ShieldCheck className="size-3.5" aria-hidden="true" />
        {t("tier.vetted")}
      </Badge>
    );
  }
  if (entry.trust === "community") {
    return (
      <Badge
        variant="outline"
        data-slot="speciality-tier"
        data-tier="community"
      >
        {t("tier.community")}
      </Badge>
    );
  }
  if (entry.trust === "third_party") {
    return (
      <Badge
        variant="outline"
        data-slot="speciality-tier"
        data-tier="third_party"
      >
        {t("tier.thirdParty", { source: entry.source ?? "" })}
      </Badge>
    );
  }
  return (
    <Badge variant="outline" data-slot="speciality-tier" data-tier="builtin">
      {t("tier.builtin")}
    </Badge>
  );
}

function StateBadge({ state }: { state: SpecialityState }) {
  const t = useTranslations("specialities");
  const variant =
    state === "unavailable"
      ? "destructive"
      : state === "enabled"
        ? "default"
        : state === "needs-consent"
          ? "outline"
          : "outline";
  const label =
    state === "enabled"
      ? t("state.enabled")
      : state === "needs-consent"
        ? t("state.needsConsent")
        : state === "unavailable"
          ? t("state.unavailable")
          : t("state.available");
  return (
    <Badge variant={variant} data-slot="speciality-state-badge">
      {label}
    </Badge>
  );
}

function SpecialityCard({
  entry,
  state,
  personaId,
  onEnable,
  onDisable,
  onConsented,
  tokenGetter,
}: {
  entry: SpecialityEntry;
  state: SpecialityState;
  personaId?: string;
  onEnable: () => void;
  onDisable: () => void;
  onConsented: (updated: SpecialityEntry) => void;
  tokenGetter: () => Promise<string | null>;
}) {
  const t = useTranslations("specialities");
  const enabled = state === "enabled";

  return (
    <Card size="sm" data-slot="speciality-card" data-state={state}>
      <Collapsible>
        <CollapsibleTrigger
          className="flex w-full items-center gap-3 px-3 text-left"
          aria-label={t("open", { name: entry.name })}
        >
          <span
            aria-hidden="true"
            data-slot="speciality-icon"
            className="grid size-9 shrink-0 place-items-center rounded-md bg-primary/10 text-primary"
          >
            <GraduationCap className="size-4" />
          </span>
          <span className="flex min-w-0 flex-col">
            <span className="truncate font-heading text-sm font-semibold">
              {entry.name}
            </span>
            <span className="truncate text-xs text-muted-foreground">
              {entry.description}
            </span>
          </span>
          <span className="ml-auto flex shrink-0 items-center gap-1.5">
            <StateBadge state={state} />
            <TierBadge entry={entry} />
          </span>
        </CollapsibleTrigger>

        <CollapsibleContent>
          <div
            className="flex flex-col gap-3 px-3 pt-3"
            data-slot="speciality-detail"
          >
            <p
              className="text-sm text-muted-foreground"
              data-slot="speciality-capability"
            >
              {t("capability")}
            </p>
            {entry.when_to_use ? (
              <p
                className="text-xs text-muted-foreground"
                data-slot="speciality-when"
              >
                {t("whenToUse", { text: entry.when_to_use })}
              </p>
            ) : null}
            <TrustDisclosure entry={entry} />

            {/* Enablement / consent — see-then-grant: this is the ONLY place the
                enable control lives, reached by expanding the detail. */}
            {enabled ? (
              <ToggleButton enabled onClick={onDisable} />
            ) : needsConsent(entry) ? (
              <ConsentGate
                entry={entry}
                personaId={personaId}
                stale={entry.consent_state === "stale"}
                onEnable={onEnable}
                onConsented={onConsented}
                tokenGetter={tokenGetter}
              />
            ) : (
              <ToggleButton enabled={false} onClick={onEnable} />
            )}
          </div>
        </CollapsibleContent>
      </Collapsible>
    </Card>
  );
}

/** The full trust disclosure in the detail — what this speciality IS + where it's from. */
function TrustDisclosure({ entry }: { entry: SpecialityEntry }) {
  const t = useTranslations("specialities");
  return (
    <dl
      className="flex flex-col gap-1 text-xs text-muted-foreground"
      data-slot="speciality-trust"
    >
      <TierBadge entry={entry} />
      {entry.source ? (
        <p>
          {entry.source_ref
            ? t("trust.sourceCommit", {
                project: entry.source,
                commit: entry.source_ref.slice(0, 12),
              })
            : t("trust.source", { project: entry.source })}
        </p>
      ) : null}
    </dl>
  );
}

function ToggleButton({
  enabled,
  onClick,
}: {
  enabled: boolean;
  onClick: () => void;
}) {
  const t = useTranslations("specialities");
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={enabled}
      data-slot="speciality-toggle"
      className={cn(
        "inline-flex w-fit items-center gap-1.5 rounded-md border px-3 py-1.5 text-sm transition-colors",
        enabled
          ? "border-primary/40 bg-primary/10 text-primary"
          : "border-border text-muted-foreground hover:border-primary/30",
      )}
    >
      {enabled ? <Check className="size-3.5" aria-hidden="true" /> : null}
      {enabled ? t("enable.disable") : t("enable.enable")}
    </button>
  );
}

/**
 * The honest-not-alarmist consent step (S3-D-5, candidate A). Shown for a
 * community/third_party speciality that is not yet consented at the current hash.
 * Names what it is (instructions the persona follows), where it's from, that it's
 * unreviewed, de-alarms ("most are helpful"), and is reversible — then grants.
 */
function ConsentGate({
  entry,
  personaId,
  stale,
  onEnable,
  onConsented,
  tokenGetter,
}: {
  entry: SpecialityEntry;
  personaId?: string;
  stale: boolean;
  onEnable: () => void;
  onConsented: (updated: SpecialityEntry) => void;
  tokenGetter: () => Promise<string | null>;
}) {
  const t = useTranslations("specialities");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState(false);
  const tierWord =
    entry.trust === "community"
      ? t("tier.communityWord")
      : t("tier.thirdPartyWord");

  async function grant() {
    if (submitting) return;
    // No persona yet (author flow): consent binds to a persona, so defer it —
    // enable the declaration; it stays inert until consented in the editor.
    if (!personaId) {
      onEnable();
      return;
    }
    setSubmitting(true);
    setError(false);
    try {
      const updated = await recordSpecialityConsent(
        personaId,
        entry.name,
        true,
        tokenGetter,
      );
      onConsented(updated);
    } catch {
      setError(true);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div
      className="flex flex-col gap-2 rounded-md border border-border bg-muted/40 p-2"
      data-slot="speciality-consent"
    >
      <p
        className="text-xs text-muted-foreground"
        data-slot="speciality-consent-copy"
      >
        {stale
          ? t("consent.regate")
          : t("consent.disclosure", {
              tier: tierWord,
              source: entry.source ?? "",
            })}
      </p>
      {!personaId ? (
        <p
          className="text-xs text-muted-foreground"
          data-slot="speciality-consent-deferred"
        >
          {t("consent.deferred")}
        </p>
      ) : null}
      <button
        type="button"
        onClick={() => void grant()}
        disabled={submitting}
        data-slot="speciality-consent-grant"
        className={cn(
          "inline-flex w-fit items-center gap-1.5 rounded-md border px-3 py-1.5 text-sm transition-colors",
          "border-primary/40 bg-primary/10 text-primary hover:bg-primary/15",
        )}
      >
        {submitting
          ? t("consent.granting")
          : stale
            ? t("consent.regrant")
            : t("consent.grant")}
      </button>
      {error ? (
        <p
          className="text-xs text-destructive"
          data-slot="speciality-consent-error"
        >
          {t("consent.error")}
        </p>
      ) : null}
    </div>
  );
}

/** A declared speciality the catalog dropped — graceful tombstone, one-click remove. */
function UnavailableSpeciality({
  name,
  onRemove,
}: {
  name: string;
  onRemove: () => void;
}) {
  const t = useTranslations("specialities");
  return (
    <Card size="sm" data-slot="speciality-card" data-state="unavailable">
      <div className="flex items-center gap-3 px-3">
        <span className="flex min-w-0 flex-col py-2">
          <span className="truncate font-heading text-sm font-semibold">
            {name}
          </span>
          <span className="truncate text-xs text-destructive">
            {t("unavailable.summary")}
          </span>
        </span>
        <button
          type="button"
          onClick={onRemove}
          data-slot="speciality-remove"
          className="ml-auto shrink-0 rounded-md border border-border px-3 py-1.5 text-sm text-muted-foreground hover:border-primary/30"
        >
          {t("unavailable.remove")}
        </button>
      </div>
    </Card>
  );
}
