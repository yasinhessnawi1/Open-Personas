"use client";

import {
  Code2,
  Cpu,
  Save,
  Settings2,
  SlidersHorizontal,
  Sparkles,
} from "lucide-react";
import dynamic from "next/dynamic";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useRef, useState } from "react";
import { useAuth } from "@/auth";
import {
  AutonomyConsentSection,
  type AutonomyLevel,
} from "@/components/persona/autonomy-consent-section";
import { PersonaModelPicker } from "@/components/persona/persona-model-picker";
import { buttonVariants } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import type { ClarifyingQuestion } from "@/lib/api";
import { getModels, type ModelOption } from "@/lib/api/models-client";
import {
  docToYaml,
  type PersonaDoc,
  readIdentity,
  readPreferredModel,
  readStringList,
  writePreferredModel,
  writeStringList,
  yamlToDoc,
} from "@/lib/persona-draft";
import { cn } from "@/lib/utils";
import { AvatarEditor } from "./avatar-editor";
import { ByoMcpManager } from "./byo-mcp-manager";
import {
  CollapsibleSection,
  SectionGroup,
  SectionTimelineNav,
} from "./collapsible-section";
import type { McpConnectionStatus } from "./mcp-connection-label";
import { type McpCatalogEntry, PersonaForm } from "./persona-form";
import {
  applyRecommendation,
  recommendationApplied,
  SuggestCapabilities,
  type ToolRecommendation,
} from "./suggest-capabilities";

const AUTONOMY_LEVELS: readonly AutonomyLevel[] = [
  "cautious",
  "balanced",
  "decisive",
];

// Spec M1 (M1-T7) — the Model section self-fetches the catalog client-side,
// same Clerk JWT-template convention as VoiceSelector / SpecialitiesChooser /
// useApi().
const MODEL_TEMPLATE = process.env.NEXT_PUBLIC_CLERK_JWT_TEMPLATE;

/** Read the doc's autonomy field, defaulting to the schema default "cautious". */
function readAutonomy(doc: PersonaDoc): AutonomyLevel {
  const value = doc.autonomy;
  return typeof value === "string" &&
    (AUTONOMY_LEVELS as readonly string[]).includes(value)
    ? (value as AutonomyLevel)
    : "cautious";
}

// Monaco is lazy + client-only so it never enters the chat-page bundle (D-09-8).
const YAMLEditor = dynamic(() => import("./yaml-editor"), {
  ssr: false,
  loading: () => <Skeleton className="h-[440px] w-full rounded-md" />,
});

type SaveResult = { error: string } | undefined;

/**
 * The clarifying-questions + refinement seam (spec 10, D-10-2 / D-10-5). Present
 * only in the authoring wizard (not when editing an existing persona). Answering
 * a question re-generates the draft; the wizard owns the round counter and hides
 * this once `round >= maxRounds` (the server backstops the cap).
 */
export type Refinement = {
  questions: ClarifyingQuestion[];
  round: number;
  maxRounds: number;
  refining: boolean;
  onAnswer: (question: string, answer: string, currentYaml: string) => void;
};

/**
 * The shared persona editor (T08): structured form ⇄ Monaco YAML, kept in sync
 * with the parsed object as the single source of truth (D-09-9). A form edit
 * regenerates the YAML; a YAML edit re-parses into the form, and invalid YAML
 * surfaces an error while the form keeps its last valid state (save is blocked
 * until the YAML parses).
 */
export function PersonaEditor({
  initialDoc,
  tools,
  skills,
  mcpServers = [],
  mcpConnections = [],
  personaId,
  onSave,
  saveLabel,
  refinement,
  initialConsent,
  initialAvatarUrl,
  onConsentChange,
  autosave = false,
  onSaveStatus,
  onDocChange,
  hideAvatar = false,
  avatarUrlOverride,
  nav = true,
  sectionsOpen = false,
}: {
  initialDoc: PersonaDoc;
  tools: string[];
  skills: string[];
  // Spec 30 T11 — built-in MCP servers for the unified capability section.
  mcpServers?: McpCatalogEntry[];
  // N6 merge-back — per-assigned-server connection status (GET /personas/{id}/
  // mcp-connections), threaded to the form's apps chooser for the friendly
  // connection badge. Optional: the author/new flow (no persona yet) and any
  // fail-soft fetch (older api, community without the runtime) pass nothing.
  mcpConnections?: McpConnectionStatus[];
  // Spec 30 T12 — the saved persona's id; enables the BYO-MCP manager (needs an
  // id to assign servers to). Absent in the author/new flow (no id yet). Spec 31
  // (D-31-X-autonomy-placement): also gates the autonomy + consent section —
  // when set with `onConsentChange`, the selector + toggle are surfaced (a
  // consent PATCH needs a persisted persona; the author/new flow omits them).
  personaId?: string;
  // The persona's avatar is carried on the YAML save's `avatar_url` field; the
  // editor tracks it out-of-band from the doc (it's a presentation field, not
  // schema). Absent in the author/new flow (the avatar auto-generates at create).
  onSave: (yaml: string, avatarUrl?: string | null) => Promise<SaveResult>;
  saveLabel: string;
  refinement?: Refinement;
  initialConsent?: boolean | null;
  initialAvatarUrl?: string | null;
  onConsentChange?: (granted: boolean | null) => Promise<SaveResult>;
  /** R11-B6 — the consolidated persona page: debounced save-on-change replaces
   * the Save button; status streams to the page's sticky bar. */
  autosave?: boolean;
  onSaveStatus?: (status: "saving" | "saved" | "error", error?: string) => void;
  /** Live doc echo for the page hero (name/role follow edits instantly). */
  onDocChange?: (doc: PersonaDoc) => void;
  /** The page hosts the avatar in its hero — suppress the inline block. */
  hideAvatar?: boolean;
  /** Controlled avatar (page-hosted AvatarEditor); rides the save unchanged. */
  avatarUrlOverride?: string | null;
  /** The kit page is a single column with a right rail — no timeline nav. */
  nav?: boolean;
  /** R11-B6 (owner-ruled): open every section card by default (Advanced stays
   * folded). The consolidated page sets this; the wizard keeps staged reveal. */
  sectionsOpen?: boolean;
}) {
  const t = useTranslations("author");
  const [doc, setDoc] = useState<PersonaDoc>(initialDoc);
  const [yamlText, setYamlText] = useState<string>(() => docToYaml(initialDoc));
  const [yamlError, setYamlError] = useState<string | null>(null);
  const [showYaml, setShowYaml] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  // Consent is a DB-backed tri-state persisted out-of-band from the YAML save
  // (PATCH /consent); autonomy rides the doc → YAML save like any other field.
  const [consent, setConsent] = useState<boolean | null>(
    initialConsent ?? null,
  );
  const [consentPending, setConsentPending] = useState(false);
  const [consentError, setConsentError] = useState<string | null>(null);
  // The avatar (a presentation field) rides the YAML save's `avatar_url`; a
  // replacement uploads immediately for preview and persists on Save.
  const [avatarUrl, setAvatarUrl] = useState<string | null>(
    initialAvatarUrl ?? null,
  );

  // Spec M1 (M1-T7) — the Model section's catalog (id/label/prices/recommended).
  // Self-fetched once (`scope=all`: the picker filters recommended-vs-all
  // locally, T7) — fail-open to `[]` on any transport/auth failure, so the
  // picker degrades to "Use tier default" only, never an error state (matches
  // T5's own fail-open contract). The token getter is held in a ref (not an
  // effect dependency): Clerk memoises it in production, but a fresh function
  // identity per render (as a test mock legitimately returns) would otherwise
  // re-run the effect every render (VoiceSelector's precedent for the same hazard).
  const { getToken } = useAuth();
  const getTokenRef = useRef(getToken);
  getTokenRef.current = getToken;
  const [models, setModels] = useState<ModelOption[]>([]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const token = await getTokenRef.current(
          MODEL_TEMPLATE ? { template: MODEL_TEMPLATE } : undefined,
        );
        const list = await getModels(token, "all");
        if (!cancelled) setModels(list);
      } catch {
        if (!cancelled) setModels([]);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const onFormChange = useCallback(
    (next: PersonaDoc) => {
      setDoc(next);
      setYamlText(docToYaml(next));
      setYamlError(null);
      onDocChange?.(next);
    },
    [onDocChange],
  );

  const handleConsentChange = useCallback(
    async (granted: boolean | null) => {
      if (!onConsentChange) return;
      const prev = consent;
      setConsent(granted); // optimistic
      setConsentPending(true);
      setConsentError(null);
      try {
        const result = await onConsentChange(granted);
        if (result?.error) {
          setConsent(prev); // revert
          setConsentError(result.error);
        }
      } catch {
        setConsent(prev);
        setConsentError(t("saveFailed"));
      } finally {
        setConsentPending(false);
      }
    },
    [consent, onConsentChange, t],
  );

  const onYamlChange = useCallback(
    (text: string) => {
      setYamlText(text);
      try {
        const parsed = yamlToDoc(text);
        setDoc(parsed);
        setYamlError(null);
        onDocChange?.(parsed);
      } catch (e) {
        setYamlError((e as Error).message);
      }
    },
    [onDocChange],
  );

  // Spec 30 T11 — apply a recommender pick to the persona (skill → skills list;
  // MCP → mcp:<name> in tools; else tools), keeping the YAML buffer in sync.
  const currentCapabilities = useCallback(
    () => ({
      tools: readStringList(doc, "tools"),
      skills: readStringList(doc, "skills"),
    }),
    [doc],
  );
  const applyRec = useCallback(
    (rec: ToolRecommendation) => {
      const next = applyRecommendation(rec, currentCapabilities());
      onFormChange(
        writeStringList(
          writeStringList(doc, "tools", next.tools),
          "skills",
          next.skills,
        ),
      );
    },
    [doc, currentCapabilities, onFormChange],
  );
  const identity = readIdentity(doc);
  const suggestDescription = [identity.role, identity.background]
    .filter(Boolean)
    .join(". ")
    .trim();

  const effectiveAvatar =
    avatarUrlOverride !== undefined ? avatarUrlOverride : avatarUrl;

  async function save() {
    if (saving || yamlError) return;
    setSaving(true);
    setSaveError(null);
    try {
      const result = await onSave(yamlText, effectiveAvatar);
      if (result?.error) setSaveError(result.error);
    } catch {
      setSaveError(t("saveFailed"));
    } finally {
      setSaving(false);
    }
  }

  // R11-B6 — autosave: any change to the (valid) YAML or the avatar debounces
  // into the SAME onSave door the Save button used; the page's sticky bar shows
  // Saving… ⇄ All changes saved ⇄ the honest error. First render never saves.
  const saveSeq = useRef(0);
  const firstRender = useRef(true);
  const onSaveRef = useRef(onSave);
  onSaveRef.current = onSave;
  const onSaveStatusRef = useRef(onSaveStatus);
  onSaveStatusRef.current = onSaveStatus;
  // biome-ignore lint/correctness/useExhaustiveDependencies: yamlText + avatar ARE the save inputs; refs carry the callbacks.
  useEffect(() => {
    if (!autosave) return;
    if (firstRender.current) {
      firstRender.current = false;
      return;
    }
    if (yamlError) return; // never persist a doc that doesn't parse
    const seq = ++saveSeq.current;
    onSaveStatusRef.current?.("saving");
    const timer = setTimeout(() => {
      void (async () => {
        try {
          const result = await onSaveRef.current(yamlText, effectiveAvatar);
          if (saveSeq.current !== seq) return; // a newer edit superseded this save
          if (result?.error) onSaveStatusRef.current?.("error", result.error);
          else onSaveStatusRef.current?.("saved");
        } catch {
          if (saveSeq.current === seq)
            onSaveStatusRef.current?.("error", t("saveFailed"));
        }
      })();
    }, 800);
    return () => clearTimeout(timer);
  }, [autosave, yamlText, effectiveAvatar, yamlError]);

  return (
    <SectionGroup>
      <div
        className={
          nav ? "lg:grid lg:grid-cols-[14rem_1fr] lg:gap-6" : undefined
        }
      >
        {nav ? <SectionTimelineNav /> : null}
        <div className="flex min-w-0 flex-col gap-5">
          {/* Avatar — existing-persona edit only (upload needs a persisted id). */}
          {personaId && !hideAvatar ? (
            <AvatarEditor
              personaId={personaId}
              name={identity.name}
              avatarUrl={avatarUrl}
              onChange={setAvatarUrl}
            />
          ) : null}

          <PersonaForm
            doc={doc}
            onChange={onFormChange}
            tools={tools}
            skills={skills}
            mcpServers={mcpServers}
            mcpConnections={mcpConnections}
            personaId={personaId}
            openAll={sectionsOpen}
          />

          {/* Model — Spec M1 (M1-T7): pick a specific priced model for this
              persona, or leave it on the tier default. Rides
              `routing.preferred_model` (T1, additive-optional) — NOT the
              retired P9 tuning surface (P9-D-5): a direct model choice, not a
              tier/weights/budget dial. Renders in BOTH the create wizard and
              the edit flow (no personaId gate — the choice rides the YAML
              itself; no separate PATCH needed here). */}
          <CollapsibleSection
            id="model"
            title={t("modelTitle")}
            icon={Cpu}
            defaultOpen={sectionsOpen}
          >
            <p className="type-caption text-muted-foreground">
              {t("modelHint")}
            </p>
            <PersonaModelPicker
              models={models}
              value={readPreferredModel(doc)}
              onSelect={(id) => onFormChange(writePreferredModel(doc, id))}
            />
          </CollapsibleSection>

          {/* Autonomy + consent: existing-persona edit only (D-31-X-autonomy-placement). */}
          {personaId && onConsentChange ? (
            <CollapsibleSection
              id="autonomy"
              title={t("autonomyTitle")}
              icon={SlidersHorizontal}
              defaultOpen={sectionsOpen}
            >
              <AutonomyConsentSection
                autonomy={readAutonomy(doc)}
                onAutonomyChange={(level) =>
                  onFormChange({ ...doc, autonomy: level })
                }
                consent={consent}
                onConsentChange={(granted) => void handleConsentChange(granted)}
                pending={consentPending}
              />
              {consentError ? (
                <p className="mt-2 text-sm text-destructive">
                  {t("saveError", { error: consentError })}
                </p>
              ) : null}
            </CollapsibleSection>
          ) : null}

          <SuggestCapabilities
            description={suggestDescription}
            onApply={applyRec}
            isApplied={(rec) =>
              recommendationApplied(rec, currentCapabilities())
            }
          />

          {/* Spec-10 seam: clarifying questions + refinement (D-10-2 / D-10-5).
              Authoring-only; a collapsible section that opens by default so it
              is the second open card in the authoring flow. */}
          {refinement && refinement.round >= refinement.maxRounds ? (
            <p className="rounded-md border border-dashed px-3 py-2 text-xs text-muted-foreground">
              {t("refineLimitReached")}
            </p>
          ) : refinement && refinement.questions.length > 0 ? (
            <CollapsibleSection
              id="refine"
              title={t("questionsTitle")}
              defaultOpen
              icon={Sparkles}
              accent="var(--primary)"
            >
              <RefineQuestions refinement={refinement} currentYaml={yamlText} />
            </CollapsibleSection>
          ) : null}

          {/* Advanced options — BYO MCP servers + raw YAML, folded away so the
              main editor stays clean (Spec 35). Collapsed by default. */}
          <CollapsibleSection
            id="advanced"
            title={t("advancedTitle")}
            icon={Settings2}
          >
            {/* Raw YAML first: a fixed toggle position so it never drifts as the
                MCP list below grows for power users. */}
            <div className="flex flex-col gap-2">
              <button
                type="button"
                onClick={() => setShowYaml((v) => !v)}
                className="inline-flex w-fit items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground"
              >
                <Code2 className="size-4" />
                {showYaml ? t("hideRawYaml") : t("editRawYaml")}
              </button>
              {showYaml ? (
                <>
                  <YAMLEditor value={yamlText} onChange={onYamlChange} />
                  {yamlError ? (
                    <p className="text-sm text-destructive">
                      {t("yamlInvalid", { error: yamlError })}
                    </p>
                  ) : null}
                </>
              ) : null}
            </div>
            {personaId ? (
              <div className="border-border border-t pt-4">
                <ByoMcpManager personaId={personaId} bare />
              </div>
            ) : null}
          </CollapsibleSection>

          {autosave ? null : (
            <div className="flex items-center justify-end gap-3">
              {saveError ? (
                <p className="flex-1 text-sm text-destructive">
                  {t("saveError", { error: saveError })}
                </p>
              ) : null}
              <button
                type="button"
                onClick={() => void save()}
                disabled={saving || yamlError !== null}
                className={cn(buttonVariants(), "gap-2")}
              >
                <Save className="size-4" />
                {saving ? t("saving") : saveLabel}
              </button>
            </div>
          )}
        </div>
      </div>
    </SectionGroup>
  );
}

function RefineQuestions({
  refinement,
  currentYaml,
}: {
  refinement: Refinement;
  currentYaml: string;
}) {
  const t = useTranslations("author");
  const [answers, setAnswers] = useState<Record<number, string>>({});

  return (
    <div className="flex flex-col gap-3">
      <p className="type-caption flex items-center gap-1.5 text-muted-foreground">
        <Sparkles className="size-3.5 text-primary" aria-hidden />
        {t("questionsHint", { max: refinement.maxRounds })}
      </p>
      <ul className="flex flex-col gap-2.5">
        {refinement.questions.map((q, i) => {
          const answer = answers[i] ?? "";
          return (
            <li
              key={`${q.section}-${q.question}`}
              className="flex flex-col gap-2 rounded-lg border border-border bg-muted/30 p-3"
            >
              <span className="type-body">
                <span className="type-caption mr-1.5 rounded border border-border bg-background px-1.5 py-0.5 font-mono uppercase text-muted-foreground">
                  {q.section}
                </span>
                {q.question}
              </span>
              <div className="flex items-end gap-2">
                <Textarea
                  rows={1}
                  value={answer}
                  onChange={(e) =>
                    setAnswers((a) => ({ ...a, [i]: e.target.value }))
                  }
                  placeholder={t("answerPlaceholder")}
                  className="min-h-9 resize-none bg-background"
                />
                <button
                  type="button"
                  disabled={refinement.refining || !answer.trim()}
                  onClick={() =>
                    refinement.onAnswer(q.question, answer.trim(), currentYaml)
                  }
                  className={cn(buttonVariants(), "shrink-0")}
                >
                  {refinement.refining ? t("refining") : t("applyAnswer")}
                </button>
              </div>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
