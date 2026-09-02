"use client";

import { Plus, Settings2, ShieldCheck, Sparkles, X } from "lucide-react";
import { useTranslations } from "next-intl";
import type { CSSProperties } from "react";
import { PersonaAvatar } from "@/components/persona/persona-avatar";
import { buttonVariants } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  type PersonaDoc,
  readIdentity,
  readSelfFacts,
  readWorldview,
  writeIdentityField,
  writeSelfFacts,
  writeWorldview,
} from "@/lib/persona-draft";
import { SAFETY_CONSTRAINT } from "@/lib/persona-safety";
import { cn } from "@/lib/utils";

/**
 * The quick-edit preview (Spec 36 design's "Choose & edit" draft card).
 *
 * A lightweight inline editor for the ESSENTIALS — name, role, background, the
 * self_facts / worldview memory lines, and constraints — so a picked starter
 * can be tweaked and created **directly**, skipping the full editor. It is NOT a
 * replacement for the full editor: tools / skills / MCP / voice / routing /
 * avatar live there. "Open full editor" hands the SAME doc to `PersonaEditor`,
 * so every quick edit carries over (the wizard owns `doc`; this component edits
 * it through `onChange`).
 *
 * Tokens only: store badges use the `--store-*` colour vocabulary; the avatar +
 * identity tint derive from the persona id via `PersonaAvatar`.
 */
export function QuickEditCard({
  doc,
  seedId,
  onChange,
  onCreate,
  onOpenFullEditor,
  creating,
  error,
}: {
  doc: PersonaDoc;
  /** Stable seed for the avatar identity colour (the starter id, or "scratch"). */
  seedId: string;
  onChange: (doc: PersonaDoc) => void;
  onCreate: () => void;
  onOpenFullEditor: () => void;
  creating: boolean;
  error: string | null;
}) {
  const t = useTranslations("author");
  const identity = readIdentity(doc);
  const selfFacts = readSelfFacts(doc);
  const worldview = readWorldview(doc);
  const name = identity.name.trim() || t("directTitle");

  const setFactLine = (list: string[]) =>
    onChange(
      writeSelfFacts(
        doc,
        list.map((fact, i) => ({
          fact,
          confidence: selfFacts[i]?.confidence ?? 1,
        })),
      ),
    );
  const setClaimLine = (list: string[]) =>
    onChange(
      writeWorldview(
        doc,
        list.map((claim, i) => ({
          claim,
          domain: worldview[i]?.domain ?? "",
          epistemic: worldview[i]?.epistemic ?? "belief",
          confidence: worldview[i]?.confidence ?? 0.8,
          valid_time: worldview[i]?.valid_time ?? "always",
        })),
      ),
    );

  return (
    <div
      data-slot="quick-edit-card"
      className="grid grid-cols-1 gap-5 lg:grid-cols-[1fr_19rem]"
    >
      {/* Editable draft */}
      <div className="rounded-2xl bg-card p-5 ring-1 ring-foreground/10 sm:p-6">
        <div className="flex items-center gap-4 border-b border-border pb-4">
          <PersonaAvatar
            persona={{ id: seedId, name: identity.name }}
            size="lg"
          />
          <div className="flex min-w-0 flex-1 flex-col gap-1">
            <p className="type-caption font-mono text-muted-foreground uppercase">
              {t("quickEyebrow")}
            </p>
            {/* Name + role read as plain text and reveal an edit affordance on
                hover/focus — no resting box (the base Input's dark fill is
                cleared). */}
            <Input
              value={identity.name}
              onChange={(e) =>
                onChange(writeIdentityField(doc, "name", e.target.value))
              }
              aria-label={t("name")}
              className="type-heading -mx-2 h-auto rounded-md border-0 bg-transparent px-2 py-0.5 leading-tight shadow-none transition-colors hover:bg-muted dark:bg-transparent dark:hover:bg-muted"
              data-slot="quick-name"
            />
            <Input
              value={identity.role}
              onChange={(e) =>
                onChange(writeIdentityField(doc, "role", e.target.value))
              }
              aria-label={t("role")}
              className="type-ui -mx-2 h-auto rounded-md border-0 bg-transparent px-2 py-0.5 text-muted-foreground shadow-none transition-colors hover:bg-muted dark:bg-transparent dark:hover:bg-muted"
            />
          </div>
          <span className="type-caption shrink-0 rounded-full bg-muted px-2.5 py-1 font-mono text-muted-foreground uppercase">
            {t("editableChip")}
          </span>
        </div>

        {/* Background — a first-class required field (not in the prototype sketch). */}
        <Field label={t("background")}>
          <Textarea
            value={identity.background}
            rows={3}
            onChange={(e) =>
              onChange(writeIdentityField(doc, "background", e.target.value))
            }
            aria-label={t("background")}
            className="resize-none"
          />
        </Field>

        <StoreLines
          badge="SF"
          color="var(--store-self-facts)"
          label={t("selfFactsTitle")}
          items={selfFacts.map((f) => f.fact)}
          addLabel={t("addSelfFact")}
          placeholder={t("fact")}
          onChange={setFactLine}
        />
        <StoreLines
          badge="WV"
          color="var(--store-worldview)"
          label={t("worldviewTitle")}
          items={worldview.map((w) => w.claim)}
          addLabel={t("addClaim")}
          placeholder={t("claim")}
          onChange={setClaimLine}
        />
        <StoreLines
          badge=""
          color="var(--primary)"
          icon
          label={t("constraints")}
          items={identity.constraints}
          addLabel={t("addConstraint")}
          placeholder={t("constraintPlaceholder")}
          lockedItem={SAFETY_CONSTRAINT}
          lockedLabel={t("safetyConstraintLocked")}
          onChange={(list) =>
            onChange(writeIdentityField(doc, "constraints", list))
          }
        />
      </div>

      {/* Right rail: summary + create + open full editor */}
      <aside className="flex flex-col gap-4 lg:sticky lg:top-4 lg:self-start">
        <div className="rounded-2xl bg-card p-5 ring-1 ring-foreground/10">
          <div className="type-ui flex flex-col gap-1.5 font-mono text-muted-foreground">
            <span>
              {t("quickSummary", {
                self: selfFacts.length,
                world: worldview.length,
              })}
            </span>
            <span>
              {t("quickConstraintsCount", {
                count: identity.constraints.length,
              })}
            </span>
          </div>
          {error ? (
            <p
              className="type-ui mt-3 text-destructive"
              role="alert"
              data-slot="quick-edit-error"
            >
              {error}
            </p>
          ) : null}
          <button
            type="button"
            onClick={onCreate}
            disabled={creating}
            className={cn(buttonVariants(), "mt-4 w-full gap-2")}
            data-slot="quick-create"
          >
            <Sparkles className="size-4" aria-hidden="true" />
            {t("createNamed", { name })}
          </button>
          <button
            type="button"
            onClick={onOpenFullEditor}
            disabled={creating}
            className={cn(
              buttonVariants({ variant: "outline" }),
              "mt-2.5 w-full gap-2",
            )}
            data-slot="quick-open-full"
          >
            <Settings2 className="size-4" aria-hidden="true" />
            {t("openFullEditor")}
          </button>
          <p className="type-caption mt-2 text-muted-foreground">
            {t("quickFullEditorHint")}
          </p>
        </div>
      </aside>
    </div>
  );
}

function Field({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="mt-5 flex flex-col gap-1.5">
      <span className="type-caption font-mono text-muted-foreground uppercase">
        {label}
      </span>
      {children}
    </div>
  );
}

/**
 * A typed-memory store row: a coloured badge + label + editable lines. A
 * `lockedItem` (the safety constraint) renders read-only + non-removable.
 */
function StoreLines({
  badge,
  color,
  icon,
  label,
  items,
  addLabel,
  placeholder,
  lockedItem,
  lockedLabel,
  onChange,
}: {
  badge: string;
  color: string;
  icon?: boolean;
  label: string;
  items: string[];
  addLabel: string;
  placeholder: string;
  lockedItem?: string;
  lockedLabel?: string;
  onChange: (list: string[]) => void;
}) {
  const t = useTranslations("author");
  return (
    <div className="mt-5 grid grid-cols-[auto_1fr] gap-3">
      <span
        aria-hidden="true"
        style={{ background: color } as CSSProperties}
        className="type-caption grid size-7 place-items-center rounded-md font-mono text-white"
      >
        {icon ? <ShieldCheck className="size-3.5" /> : badge}
      </span>
      <div className="min-w-0">
        <div className="type-ui font-mono font-medium">
          {label} · {items.length}
        </div>
        <ul className="mt-2 flex flex-col gap-1.5">
          {items.map((item, i) => {
            const locked = lockedItem !== undefined && item === lockedItem;
            return (
              // biome-ignore lint/suspicious/noArrayIndexKey: positional rows
              <li key={i} className="flex items-center gap-2">
                <span
                  aria-hidden="true"
                  style={{ background: color } as CSSProperties}
                  className="size-1.5 shrink-0 rounded-full"
                />
                <Input
                  value={item}
                  placeholder={placeholder}
                  readOnly={locked}
                  aria-label={`${label} ${i + 1}`}
                  className="h-8 flex-1"
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
                    aria-label={lockedLabel}
                    title={lockedLabel}
                    className="grid size-7 shrink-0 place-items-center text-primary"
                  >
                    <ShieldCheck className="size-4" />
                  </span>
                ) : (
                  <button
                    type="button"
                    aria-label={t("remove")}
                    onClick={() => onChange(items.filter((_, j) => j !== i))}
                    className="grid size-7 shrink-0 place-items-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground"
                  >
                    <X className="size-3.5" />
                  </button>
                )}
              </li>
            );
          })}
        </ul>
        <button
          type="button"
          onClick={() => onChange([...items, ""])}
          className="type-ui mt-1.5 inline-flex items-center gap-1.5 rounded-md px-1.5 py-1 text-muted-foreground hover:bg-muted hover:text-primary"
        >
          <Plus className="size-3.5" />
          {addLabel}
        </button>
      </div>
    </div>
  );
}
