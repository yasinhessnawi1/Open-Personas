"use client";

import { Lock, Mic, Plus, ShieldCheck, Wrench, X } from "lucide-react";
import { useTranslations } from "next-intl";
import type { ComponentType } from "react";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
// Spec V6 C2 — the voice-selector contribution (F5/Spec 10 own the screen).
import { VoiceSelector } from "@/components/voice/voice-selector";
import {
  EPISTEMIC_OPTIONS,
  type PersonaDoc,
  readIdentity,
  readSelfFacts,
  readStringList,
  readWorldview,
  writeIdentityField,
  writeSelfFacts,
  writeStringList,
  writeWorldview,
} from "@/lib/persona-draft";
import { SAFETY_CONSTRAINT } from "@/lib/persona-safety";
import { cn } from "@/lib/utils";
import { voiceLanguageWarning } from "@/lib/voice/language-support";
import { AppsChooser } from "./apps-chooser";
import { CollapsibleSection } from "./collapsible-section";
import type { McpConnectionStatus } from "./mcp-connection-label";
import { SpecialitiesChooser } from "./specialities-chooser";

// Spec 30 T11 — a built-in MCP server in the capability-management catalog.
// A persona enables a server by carrying `mcp:<name>` in its `tools` list.
//
// N3 (MCP-as-apps): the entry now also carries the Docker catalog-mirror display
// metadata + trust labels + the display-only credential schema the apps UX renders
// (icon/friendly-name/description for the chooser; signed/source/risk/allowHosts for
// the legible-not-opaque trust disclosure; secrets[] for the read-honest needs-setup
// affordance — N3-D-6/8/10). All are READ-only inputs; N3 never writes a secret
// (the secret-write path is N4's, N3-D-1). Every field is additive-with-default so a
// row from the old five-field contract still maps cleanly.
export interface McpCatalogSecret {
  name: string;
  env: string;
  example: string;
  description: string;
}

export interface McpCatalogEntry {
  name: string;
  description: string;
  provider: string;
  defaultEnabled: boolean;
  requiredEnv: string[];
  // -- N3: Docker catalog-mirror display metadata + trust labels + secret schema --
  displayName: string;
  iconUrl: string;
  image: string;
  serverType: string;
  risk: string;
  sourceProject: string;
  sourceCommit: string;
  signed: boolean;
  allowHosts: string[];
  secrets: McpCatalogSecret[];
  // -- Spec R8/N7: per-user OAuth binding passthrough --
  // `authMethod === "oauth"` marks a catalog entry whose connection is obtained
  // per-user via Connect (N7-T3), never a credential form. Empty = the pre-N7
  // env/credential/none path.
  authMethod: string;
  oauthProvider: string;
}

/**
 * Spec N7 (D-N7-2) — which MCP mechanisms THIS DEPLOYMENT can actually run.
 *
 * Rides `GET /v1/mcp-catalog` as a sibling of the server list (the wrapper
 * response). Threaded down to the apps chooser so it never guesses from
 * indirect signals: `perTenantRuntime` gates whether an image-type
 * (`serverType === "server"`) app can be adopted at all; `gateway` distinguishes
 * "the operator may have exposed it on their gateway" from "nothing can serve
 * this"; `oauthProviders` drives the BYO manager's fail-closed provider select
 * (N7-T3b). All default OFF so every existing caller that doesn't pass this prop
 * renders EXACTLY the pre-N7 behavior (no image-app adopt gate widening, no
 * oauth option surfaced beyond the always-on mcp-native path).
 */
export interface McpDeploymentCapabilities {
  perTenantRuntime: boolean;
  gateway: boolean;
  oauthProviders: string[];
}

/** The default, all-off capabilities — pre-N7 behavior for callers that don't
 * (yet) thread the real deployment signal. */
export const MCP_CAPABILITIES_OFF: McpDeploymentCapabilities = {
  perTenantRuntime: false,
  gateway: false,
  oauthProviders: [],
};

// Spec 30 — the accuracy-preserving combined cap across tools + skills + MCP
// (the tool-count-cliff, Spec 26 D-26): communicated, not hard-enforced.
const CAPABILITY_SOFT_CAP = 10;

// The structured persona editor (T08). Controlled: it renders from `doc` and
// emits a new `doc` on every edit. The parent keeps the YAML buffer in sync.
export function PersonaForm({
  doc,
  onChange,
  tools,
  mcpServers = [],
  mcpConnections = [],
  mcpCapabilities = MCP_CAPABILITIES_OFF,
  personaId,
  openAll = false,
}: {
  doc: PersonaDoc;
  onChange: (doc: PersonaDoc) => void;
  tools: string[];
  // Spec S3: the available-skill names are no longer consumed here — the
  // SpecialitiesChooser self-fetches the tier-aware catalog + consent state. Kept
  // in the prop shape so existing callers/tests pass unchanged.
  skills?: string[];
  // Spec 30 T11 — built-in MCP servers (from GET /v1/mcp-catalog). Optional so
  // existing callers/tests that don't pass it render tools+skills unchanged.
  mcpServers?: McpCatalogEntry[];
  // N6 merge-back — per-assigned-server connection status, threaded to the apps
  // chooser so each MCP app card shows its friendly connection badge. Optional so
  // existing callers/tests (and the author/new flow) render unchanged.
  mcpConnections?: McpConnectionStatus[];
  // Spec N7 (D-N7-2) — which MCP mechanisms this deployment can run, threaded to
  // the apps chooser's adopt gate + image-toggle suppression. Optional, defaults
  // all-off (the pre-N7 behavior) so existing callers/tests render unchanged.
  mcpCapabilities?: McpDeploymentCapabilities;
  // Spec N4 (Group D) — the persona being edited, threaded to the apps chooser so a
  // remote app that declares a credential can render the setup form. Absent in the
  // author/new flow (no id yet) → the read-honest needs-setup disclosure.
  personaId?: string;
  /** R11-B6 (owner-ruled): the consolidated page opens EVERY card by default
   * (only Advanced stays folded); the authoring wizard keeps the staged flow. */
  openAll?: boolean;
}) {
  const t = useTranslations("author");
  const tApps = useTranslations("apps");
  const tSpecialities = useTranslations("specialities");
  const identity = readIdentity(doc);
  // The persona's current voice id (identity.voice.voice_id), if set — V6 C2.
  const identityRecord = doc.identity as Record<string, unknown> | undefined;
  const voiceRecord = identityRecord?.voice as
    | { voice_id?: unknown }
    | null
    | undefined;
  const currentVoiceId =
    voiceRecord && typeof voiceRecord.voice_id === "string"
      ? voiceRecord.voice_id
      : null;
  const selfFacts = readSelfFacts(doc);
  const worldview = readWorldview(doc);
  const declaredTools = readStringList(doc, "tools");
  const declaredSkills = readStringList(doc, "skills");
  // The combined capability count (tools — incl. mcp: entries — plus skills).
  const capabilityCount = declaredTools.length + declaredSkills.length;

  return (
    <div className="flex flex-col gap-5">
      {/* Identity */}
      <Section
        id="identity"
        title={t("identityTitle")}
        defaultOpen
        badge="ID"
        accent="var(--store-identity)"
      >
        <Field label={t("name")}>
          <Input
            value={identity.name}
            onChange={(e) =>
              onChange(writeIdentityField(doc, "name", e.target.value))
            }
          />
        </Field>
        <Field label={t("role")}>
          <Input
            value={identity.role}
            onChange={(e) =>
              onChange(writeIdentityField(doc, "role", e.target.value))
            }
          />
        </Field>
        <Field label={t("background")}>
          <GhostTextarea
            value={identity.background}
            rows={4}
            onChange={(e) =>
              onChange(writeIdentityField(doc, "background", e.target.value))
            }
          />
        </Field>
        <Field label={t("language")}>
          <Input
            value={identity.language_default}
            className="max-w-28"
            onChange={(e) =>
              onChange(
                writeIdentityField(doc, "language_default", e.target.value),
              )
            }
          />
          {voiceLanguageWarning(identity.language_default) !== null ? (
            <output className="type-caption mt-1 block text-amber-600">
              {voiceLanguageWarning(identity.language_default)}
            </output>
          ) : null}
        </Field>
        <Field label={t("constraints")}>
          <ListEditor
            items={identity.constraints}
            placeholder={t("constraintPlaceholder")}
            addLabel={t("addConstraint")}
            // The mandatory safety constraint is pinned: read-only, not
            // removable (Spec 36, D-36-safety-ux). The server re-asserts it
            // regardless; this stops a user editing it away in the form.
            lockedItem={SAFETY_CONSTRAINT}
            lockedLabel={t("safetyConstraintLocked")}
            onChange={(list) =>
              onChange(writeIdentityField(doc, "constraints", list))
            }
          />
        </Field>
      </Section>

      {/* Self-facts */}
      <Section
        defaultOpen={openAll}
        id="self-facts"
        title={t("selfFactsTitle")}
        badge="SF"
        accent="var(--store-self-facts)"
      >
        <StoreItems accent="var(--store-self-facts)">
          {selfFacts.map((f, i) => (
            <StoreRow
              // biome-ignore lint/suspicious/noArrayIndexKey: rows are positional
              key={i}
              removeLabel={t("remove")}
              onRemove={() =>
                onChange(
                  writeSelfFacts(
                    doc,
                    selfFacts.filter((_, j) => j !== i),
                  ),
                )
              }
              trailing={
                <Confidence
                  value={f.confidence}
                  onChange={(v) =>
                    onChange(
                      writeSelfFacts(
                        doc,
                        selfFacts.map((x, j) =>
                          j === i ? { ...x, confidence: v } : x,
                        ),
                      ),
                    )
                  }
                />
              }
            >
              <GhostInput
                value={f.fact}
                placeholder={t("fact")}
                onChange={(e) =>
                  onChange(
                    writeSelfFacts(
                      doc,
                      selfFacts.map((x, j) =>
                        j === i ? { ...x, fact: e.target.value } : x,
                      ),
                    ),
                  )
                }
              />
            </StoreRow>
          ))}
          <AddButton
            label={t("addSelfFact")}
            onClick={() =>
              onChange(
                writeSelfFacts(doc, [
                  ...selfFacts,
                  { fact: "", confidence: 1 },
                ]),
              )
            }
          />
        </StoreItems>
      </Section>

      {/* Worldview */}
      <Section
        defaultOpen={openAll}
        id="worldview"
        title={t("worldviewTitle")}
        badge="WV"
        accent="var(--store-worldview)"
      >
        <StoreItems accent="var(--store-worldview)">
          {worldview.map((w, i) => {
            const set = (patch: Partial<typeof w>) =>
              onChange(
                writeWorldview(
                  doc,
                  worldview.map((x, j) => (j === i ? { ...x, ...patch } : x)),
                ),
              );
            return (
              <StoreRow
                // biome-ignore lint/suspicious/noArrayIndexKey: rows are positional
                key={i}
                removeLabel={t("remove")}
                onRemove={() =>
                  onChange(
                    writeWorldview(
                      doc,
                      worldview.filter((_, j) => j !== i),
                    ),
                  )
                }
                trailing={
                  <EpistemicChip
                    value={w.epistemic}
                    onChange={(v) => set({ epistemic: v })}
                  />
                }
              >
                <GhostInput
                  value={w.claim}
                  placeholder={t("claim")}
                  onChange={(e) => set({ claim: e.target.value })}
                />
                {/* the detail the kit tucks under the claim — a quiet mono meta row */}
                <div className="flex flex-wrap items-center gap-x-4 gap-y-1 pl-1.5">
                  <MetaField label={t("domain")}>
                    <GhostInput
                      value={w.domain}
                      placeholder={t("domain")}
                      className="w-28 text-xs"
                      onChange={(e) => set({ domain: e.target.value })}
                    />
                  </MetaField>
                  <MetaField label={t("validTime")}>
                    <GhostInput
                      value={w.valid_time}
                      placeholder={t("validTime")}
                      className="w-24 text-xs"
                      onChange={(e) => set({ valid_time: e.target.value })}
                    />
                  </MetaField>
                  <Confidence
                    value={w.confidence}
                    onChange={(v) => set({ confidence: v })}
                  />
                </div>
              </StoreRow>
            );
          })}
          <AddButton
            label={t("addClaim")}
            onClick={() =>
              onChange(
                writeWorldview(doc, [
                  ...worldview,
                  {
                    claim: "",
                    domain: "",
                    epistemic: "belief",
                    confidence: 0.8,
                    valid_time: "always",
                  },
                ]),
              )
            }
          />
        </StoreItems>
      </Section>

      {/* Voice — its own card (a persona's audible identity, V6 C2). Sits after
          the typed-memory stores so identity → self-facts → worldview group
          first, then voice (consistency). */}
      <Section
        defaultOpen={openAll}
        id="voice"
        title={t("voiceTitle")}
        icon={Mic}
      >
        <Field label={t("voice")} hint={t("voiceDescription")}>
          <VoiceSelector
            value={currentVoiceId}
            language={identity.language_default}
            onChange={(voice) =>
              onChange({
                ...doc,
                identity: {
                  ...((doc.identity ?? {}) as Record<string, unknown>),
                  voice,
                },
              })
            }
          />
        </Field>
      </Section>

      {/* Capabilities: tools + skills + MCP as one set (spec 30 T11) */}
      <Section
        defaultOpen={openAll}
        id="capabilities"
        title={t("capabilitiesTitle")}
        icon={Wrench}
      >
        <p
          className={cn(
            "text-xs",
            capabilityCount > CAPABILITY_SOFT_CAP
              ? "text-destructive"
              : "text-muted-foreground",
          )}
          data-slot="capability-count"
        >
          {t("capabilityCount", { count: capabilityCount })} ·{" "}
          {t("capabilityCapHint")}
        </p>
        {/* R4 T5 — ONE unified "Apps & Tools" menu: built-in tools + MCP apps in
            a SINGLE searchable list (one header, one search box), each by friendly
            label + a one-line what-it-does under the same see-then-grant grammar.
            Both write the persona's `tools:` list (bare names / `mcp:<name>`).
            Specialities (skills, S-track) stay a SEPARATE surface (the ratified
            not-unified decision). */}
        <Subsection title={tApps("titleCombined")}>
          <AppsChooser
            apps={mcpServers}
            tools={tools}
            declaredTools={declaredTools}
            connections={mcpConnections}
            capabilities={mcpCapabilities}
            personaId={personaId}
            onChange={(list) => onChange(writeStringList(doc, "tools", list))}
          />
        </Subsection>
        {/* Spec S3 — Specialities: skills surfaced with trust tiers + a consent
            flow, a SEPARATE surface from the apps chooser above (the not-unified
            decision). Enable/disable drives the persona's `skills:` declaration;
            the chooser self-fetches the tier-aware catalog + this persona's
            consent state. Skill names are normalised to friendly labels (R4 T5). */}
        <Subsection title={tSpecialities("title")}>
          <SpecialitiesChooser
            personaId={personaId}
            declaredSkills={declaredSkills}
            onChange={(list) => onChange(writeStringList(doc, "skills", list))}
          />
        </Subsection>
      </Section>

      {/* Spec P9 (P9-D-5): the Spec-31 RoutingSection is retired — surface→tier
          is a deliberate product policy, not a per-persona tuning surface. A
          stored `routing:` block (pins, flags) is preserved untouched by every
          writer in persona-draft (back-compat: pins stay honored server-side). */}
    </div>
  );
}

function Subsection({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-2">
      <h3 className="text-xs font-medium text-muted-foreground">{title}</h3>
      {children}
    </div>
  );
}

// A collapsible section card (Settings-style). `defaultOpen` controls the
// initial state; the editor opens only Identity, the rest start collapsed and
// expand on click (or via the left timeline nav).
function Section({
  id,
  title,
  defaultOpen,
  badge,
  accent,
  icon,
  children,
}: {
  id: string;
  title: string;
  defaultOpen?: boolean;
  badge?: string;
  accent?: string;
  icon?: ComponentType<{ className?: string; "aria-hidden"?: boolean }>;
  children: React.ReactNode;
}) {
  return (
    <CollapsibleSection
      id={id}
      title={title}
      defaultOpen={defaultOpen}
      badge={badge}
      accent={accent}
      icon={icon}
    >
      {children}
    </CollapsibleSection>
  );
}

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    // biome-ignore lint/a11y/noLabelWithoutControl: the form control is passed as children
    <label className="flex flex-col gap-1.5">
      <span className="type-caption normal-case tracking-normal text-muted-foreground">
        {label}
      </span>
      {hint ? (
        <span className="text-xs text-muted-foreground/80">{hint}</span>
      ) : null}
      {children}
    </label>
  );
}

function Confidence({
  value,
  onChange,
}: {
  value: number;
  onChange: (v: number) => void;
}) {
  return (
    <div className="flex shrink-0 items-center gap-1.5">
      <input
        type="range"
        min={0}
        max={1}
        step={0.05}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        className="w-20 accent-primary"
        aria-label="confidence"
      />
      <span className="w-8 font-mono text-xs text-muted-foreground tabular-nums">
        {value.toFixed(2)}
      </span>
    </div>
  );
}

function ListEditor({
  items,
  placeholder,
  addLabel,
  onChange,
  lockedItem,
  lockedLabel,
}: {
  items: string[];
  placeholder: string;
  addLabel: string;
  onChange: (list: string[]) => void;
  /** An item that renders read-only + non-removable (e.g. the safety constraint). */
  lockedItem?: string;
  /** Accessible label for the lock indicator on a locked row. */
  lockedLabel?: string;
}) {
  // R11-B6 pixel pass — the kit `.constraint`: a primary-tinted shield box per
  // row (the safety spine, kept even across voice + text), editable in place.
  return (
    <div className="flex flex-col gap-2">
      {items.map((item, i) => {
        const locked = lockedItem !== undefined && item === lockedItem;
        return (
          <div
            // biome-ignore lint/suspicious/noArrayIndexKey: rows are positional
            key={i}
            className="group flex items-start gap-2.5 rounded-md border border-primary/20 bg-primary/[0.04] px-3 py-2"
          >
            <ShieldCheck
              className="mt-1.5 size-4 shrink-0 text-primary"
              aria-hidden="true"
            />
            <GhostInput
              value={item}
              placeholder={placeholder}
              className="flex-1"
              readOnly={locked}
              aria-readonly={locked || undefined}
              data-locked={locked || undefined}
              onChange={
                locked
                  ? undefined
                  : (e) =>
                      onChange(
                        items.map((x, j) => (j === i ? e.target.value : x)),
                      )
              }
            />
            {locked ? (
              <span
                role="img"
                className="type-caption mt-1 grid size-6 shrink-0 place-items-center text-muted-foreground"
                title={lockedLabel}
                aria-label={lockedLabel}
              >
                <Lock className="size-3.5" />
              </span>
            ) : (
              <button
                type="button"
                aria-label="remove"
                onClick={() => onChange(items.filter((_, j) => j !== i))}
                className="mt-1 grid size-6 shrink-0 place-items-center rounded text-muted-foreground opacity-50 transition-opacity hover:text-destructive group-hover:opacity-100"
              >
                <X className="size-3.5" aria-hidden="true" />
              </button>
            )}
          </div>
        );
      })}
      <AddButton label={addLabel} onClick={() => onChange([...items, ""])} />
    </div>
  );
}

function AddButton({ label, onClick }: { label: string; onClick: () => void }) {
  // R11-B6 pixel pass — the kit `.additem`: a muted store-tinted add affordance
  // (inherits `--sc` inside a StoreItems block; falls back to the accent off-store).
  return (
    <button
      type="button"
      onClick={onClick}
      className="inline-flex w-fit items-center gap-1.5 rounded-md px-1.5 py-1.5 text-sm text-muted-foreground transition-colors hover:bg-[color-mix(in_oklch,var(--sc,var(--primary))_8%,transparent)] hover:text-[color-mix(in_oklch,var(--sc,var(--primary))_72%,var(--foreground))]"
    >
      <Plus className="size-3.5" />
      {label}
    </button>
  );
}

// ---------------------------------------------------------------------------
// R11-B6 pixel pass — the kit's typed-memory rows (persona-detail.html `.store`
// / `.item`): a store-colour dot, borderless-until-focus editable text, an
// optional trailing chip (epistemic), and a hover-revealed delete. The store
// accent rides `--sc` (set by <StoreItems>), defaulting to --primary off-store.
// ---------------------------------------------------------------------------

const GHOST_FIELD =
  "h-auto rounded border-transparent bg-transparent px-1.5 py-1 shadow-none focus-visible:border-transparent";

/** The kit `.item__text` — a borderless editable input revealing only a focus ring. */
function GhostInput({
  className,
  ...props
}: React.ComponentProps<typeof Input>) {
  return <Input className={cn(GHOST_FIELD, className)} {...props} />;
}

/** Ghost multiline field (identity background). */
function GhostTextarea({
  className,
  ...props
}: React.ComponentProps<typeof Textarea>) {
  return (
    <Textarea
      className={cn(
        GHOST_FIELD,
        "min-h-16 resize-y leading-relaxed",
        className,
      )}
      {...props}
    />
  );
}

/** The store block (kit `.store` inner tint): carries the accent as `--sc` for
 * the rows' dots + hover + the "+ Add" affordance below. */
function StoreItems({
  accent,
  children,
}: {
  accent: string;
  children: React.ReactNode;
}) {
  return (
    <div
      className="flex flex-col gap-0.5"
      style={{ "--sc": accent } as React.CSSProperties}
    >
      {children}
    </div>
  );
}

/** One typed-memory row (kit `.item`): dot + content + trailing + hover-delete. */
function StoreRow({
  children,
  trailing,
  onRemove,
  removeLabel,
}: {
  children: React.ReactNode;
  trailing?: React.ReactNode;
  onRemove?: () => void;
  removeLabel?: string;
}) {
  return (
    <div className="group flex items-start gap-2.5 rounded-md px-1 py-1 transition-colors hover:bg-[color-mix(in_oklch,var(--sc,var(--primary))_8%,transparent)]">
      <span
        aria-hidden="true"
        className="mt-[0.6rem] size-1.5 shrink-0 rounded-full"
        style={{ background: "var(--sc, var(--primary))" }}
      />
      <div className="flex min-w-0 flex-1 flex-col gap-1.5">{children}</div>
      {trailing}
      {onRemove ? (
        <button
          type="button"
          aria-label={removeLabel}
          onClick={onRemove}
          className="mt-1 grid size-6 shrink-0 place-items-center rounded text-muted-foreground opacity-0 transition-opacity hover:text-destructive focus-visible:opacity-100 group-hover:opacity-100"
        >
          <X className="size-3.5" aria-hidden="true" />
        </button>
      ) : null}
    </div>
  );
}

/** The kit `.item__epi` — the worldview epistemic marker as a mono chip. */
function EpistemicChip({
  value,
  onChange,
}: {
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      aria-label="epistemic marker"
      className="type-caption mt-0.5 h-6 shrink-0 rounded-full border border-border bg-background px-2 text-muted-foreground"
    >
      {EPISTEMIC_OPTIONS.map((opt) => (
        <option key={opt} value={opt}>
          {opt}
        </option>
      ))}
    </select>
  );
}

/** A mono label + inline control for the worldview secondary meta (domain /
 * valid-time / confidence — the detail the kit tucks under the claim). */
function MetaField({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    // biome-ignore lint/a11y/noLabelWithoutControl: the control is passed as children
    <label className="flex items-center gap-1.5">
      <span className="type-caption text-muted-foreground">{label}</span>
      {children}
    </label>
  );
}
