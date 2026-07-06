"use client";

import { Mic, Plus, ShieldCheck, Wrench, X } from "lucide-react";
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
}

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
  personaId,
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
  // Spec N4 (Group D) — the persona being edited, threaded to the apps chooser so a
  // remote app that declares a credential can render the setup form. Absent in the
  // author/new flow (no id yet) → the read-honest needs-setup disclosure.
  personaId?: string;
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
          <Textarea
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
        id="self-facts"
        title={t("selfFactsTitle")}
        badge="SF"
        accent="var(--store-self-facts)"
      >
        {selfFacts.map((f, i) => (
          // biome-ignore lint/suspicious/noArrayIndexKey: rows are positional
          <div key={i} className="flex items-start gap-2">
            <Input
              value={f.fact}
              placeholder={t("fact")}
              className="flex-1"
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
            <RemoveButton
              label={t("remove")}
              onClick={() =>
                onChange(
                  writeSelfFacts(
                    doc,
                    selfFacts.filter((_, j) => j !== i),
                  ),
                )
              }
            />
          </div>
        ))}
        <AddButton
          label={t("addSelfFact")}
          onClick={() =>
            onChange(
              writeSelfFacts(doc, [...selfFacts, { fact: "", confidence: 1 }]),
            )
          }
        />
      </Section>

      {/* Worldview */}
      <Section
        id="worldview"
        title={t("worldviewTitle")}
        badge="WV"
        accent="var(--store-worldview)"
      >
        {worldview.map((w, i) => {
          const set = (patch: Partial<typeof w>) =>
            onChange(
              writeWorldview(
                doc,
                worldview.map((x, j) => (j === i ? { ...x, ...patch } : x)),
              ),
            );
          return (
            // biome-ignore lint/suspicious/noArrayIndexKey: rows are positional
            <div key={i} className="flex flex-col gap-2 rounded-md border p-3">
              <div className="flex items-start gap-2">
                <Input
                  value={w.claim}
                  placeholder={t("claim")}
                  className="flex-1"
                  onChange={(e) => set({ claim: e.target.value })}
                />
                <RemoveButton
                  label={t("remove")}
                  onClick={() =>
                    onChange(
                      writeWorldview(
                        doc,
                        worldview.filter((_, j) => j !== i),
                      ),
                    )
                  }
                />
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <Input
                  value={w.domain}
                  placeholder={t("domain")}
                  className="w-32"
                  onChange={(e) => set({ domain: e.target.value })}
                />
                <select
                  value={w.epistemic}
                  onChange={(e) => set({ epistemic: e.target.value })}
                  className="h-9 rounded-md border border-input bg-transparent px-2 font-mono text-xs uppercase shadow-xs"
                >
                  {EPISTEMIC_OPTIONS.map((opt) => (
                    <option key={opt} value={opt}>
                      {opt}
                    </option>
                  ))}
                </select>
                <Input
                  value={w.valid_time}
                  placeholder={t("validTime")}
                  className="w-28"
                  onChange={(e) => set({ valid_time: e.target.value })}
                />
                <Confidence
                  value={w.confidence}
                  onChange={(v) => set({ confidence: v })}
                />
              </div>
            </div>
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
      </Section>

      {/* Voice — its own card (a persona's audible identity, V6 C2). Sits after
          the typed-memory stores so identity → self-facts → worldview group
          first, then voice (consistency). */}
      <Section id="voice" title={t("voiceTitle")} icon={Mic}>
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
      <Section id="capabilities" title={t("capabilitiesTitle")} icon={Wrench}>
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
      <span className="text-xs font-medium text-muted-foreground">{label}</span>
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
  return (
    <div className="flex flex-col gap-2">
      {items.map((item, i) => {
        const locked = lockedItem !== undefined && item === lockedItem;
        return (
          // biome-ignore lint/suspicious/noArrayIndexKey: rows are positional
          <div key={i} className="flex items-center gap-2">
            <Input
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
                className="grid size-8 shrink-0 place-items-center text-muted-foreground"
                title={lockedLabel}
                aria-label={lockedLabel}
              >
                <ShieldCheck className="size-4 text-primary" />
              </span>
            ) : (
              <RemoveButton
                label="remove"
                onClick={() => onChange(items.filter((_, j) => j !== i))}
              />
            )}
          </div>
        );
      })}
      <AddButton label={addLabel} onClick={() => onChange([...items, ""])} />
    </div>
  );
}

function AddButton({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="inline-flex w-fit items-center gap-1.5 text-sm text-primary hover:underline"
    >
      <Plus className="size-3.5" />
      {label}
    </button>
  );
}

function RemoveButton({
  label,
  onClick,
}: {
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      className="mt-1 shrink-0 text-muted-foreground hover:text-destructive"
    >
      <X className="size-4" />
    </button>
  );
}
