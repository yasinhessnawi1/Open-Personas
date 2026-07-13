"use client";

import { Sparkles, Wand2 } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";
import { MicDictation } from "@/components/chat/mic-dictation";
import { Stack } from "@/components/layout";
import { SkeletonLine } from "@/components/patterns/loading";
import { buttonVariants } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Textarea } from "@/components/ui/textarea";
import { DICTATION_LOCALES } from "@/i18n/config";
import type { AuthoringDraft } from "@/lib/api";
import { ApiError } from "@/lib/api/client";
import { useAuthor } from "@/lib/hooks/use-author";
import { createPersona } from "@/lib/persona-actions";
import {
  docToYaml,
  emptyPersonaDoc,
  type PersonaDoc,
  readPreferredModel,
  writePreferredModel,
  yamlToDoc,
} from "@/lib/persona-draft";
import type { PersonaExample } from "@/lib/persona-examples";
import { ensureSafetyConstraint } from "@/lib/persona-safety";
import { validatePersonaDoc } from "@/lib/persona-schema";
import { cn } from "@/lib/utils";
import { ExampleGallery } from "./example-gallery";
import { PersonaEditor } from "./persona-editor";
import type { McpCatalogEntry } from "./persona-form";
import { QuickEditCard } from "./quick-edit-card";

type Phase = "describe" | "loading" | "creating" | "review";

/**
 * Server-side cap (D-10-5). UI hides the questions after this; server backstops.
 * Preserved verbatim per audit.md §authoring.plumbing.
 */
const MAX_REFINE_ROUNDS = 3;

/**
 * Spec M1 (M1-T7) — seed a fresh/regenerated doc with the caller's sticky
 * per-user model default (`profile.preferred_model`, T6's PATCH target),
 * UNLESS the doc already carries an explicit pick (never clobber one a
 * starter/drafter/refine already authored).
 */
function withDefaultModel(
  doc: PersonaDoc,
  defaultModel: string | null | undefined,
): PersonaDoc {
  if (!defaultModel) return doc;
  if (readPreferredModel(doc) !== null) return doc;
  return writePreferredModel(doc, defaultModel);
}

/**
 * AuthorWizard — the new-persona flow (Spec 36 + Spec F2 T29 presentation).
 *
 * THREE create paths, all converging on the shared `PersonaEditor` + a single
 * direct-create assembly:
 *   1. PREBUILT STARTER (primary, Spec 36) — pick a flagship starter → its
 *      structured draft opens in the editor → Create posts it DIRECTLY to
 *      `POST /v1/personas` with NO `/author` LLM call (instant).
 *   2. START FROM SCRATCH — an empty structured draft opens in the editor.
 *   3. DESCRIBE YOUR OWN (drafter) — free text → `useAuthor()` two-endpoint
 *      flow (D-09-11 / D-10-2) → review with the clarifying-questions seam.
 *
 * Every path's Create runs through `handleCreate`: re-assert the safety
 * constraint (D-36-safety-ux), validate against the v1 schema client-side
 * (D-36-validation), then `createPersona`. The server (`_guard_safety`) remains
 * the authoritative safety floor.
 *
 * DO NOT TOUCH (per audit.md §authoring.plumbing):
 *   - `useAuthor()` hook, `MAX_REFINE_ROUNDS`, `createPersona`, the
 *     `applyDraft` / `generate` / `answerQuestion` drafter state machine,
 *     `ApiError.isRateLimited` handling, and `<PersonaEditor>` composition.
 */
export function AuthorWizard({
  tools,
  skills,
  mcpServers = [],
  defaultModel = null,
}: {
  tools: string[];
  skills: string[];
  mcpServers?: McpCatalogEntry[];
  /**
   * Spec M1 (M1-T7) — the caller's sticky per-user model default
   * (`profile.preferred_model`, already fetched app-side by the /personas/new
   * page), pre-selected on a fresh draft. `null`/omitted ⇒ no prefill (a
   * fresh account, or the caller hasn't picked one yet).
   */
  defaultModel?: string | null;
}) {
  const t = useTranslations("author");
  const { author, refine } = useAuthor();
  const [description, setDescription] = useState("");
  // R9-025a — mic dictation for the free-text description: describing the
  // persona by speaking gives the author full control over how detailed the
  // description is, without typing it all out.
  const descriptionRef = useRef<HTMLTextAreaElement>(null);
  // R9-025 reopen — context-pinned dictation: pin the mic to the app's
  // active UI locale (there is no persona yet during authoring, so this is
  // the only context signal available) rather than leaning on Deepgram's
  // detect_language. Driven by the i18n runtime (`useLocale`), NOT a
  // literal — v1 ships only "en" as a real locale, but this picks up any
  // future one automatically. "xx" (the i18n-coverage pseudo-locale) is
  // deliberately excluded via `DICTATION_LOCALES` — it is not a real
  // spoken language and must never reach the STT provider as a hint.
  const locale = useLocale();
  const dictationLanguage = DICTATION_LOCALES.includes(locale)
    ? locale
    : undefined;
  const [phase, setPhase] = useState<Phase>("describe");
  // `draft` is the DRAFTER output (carries clarifying questions); it is null for
  // the prebuilt-starter and start-from-scratch paths, which need no refinement.
  const [draft, setDraft] = useState<AuthoringDraft | null>(null);
  const [doc, setDoc] = useState<PersonaDoc | null>(null);
  const [round, setRound] = useState(0);
  const [refining, setRefining] = useState(false);
  // Spec P0 — the raw draft text streaming in during `loading`, and the visible
  // "regenerating" flag set when the validation-repair re-stream fires (so
  // attempt-2's text replaces attempt-1's rather than appending onto it).
  const [streamText, setStreamText] = useState("");
  const [streamRetrying, setStreamRetrying] = useState(false);
  const [editorKey, setEditorKey] = useState(0);
  const [error, setError] = useState<string | null>(null);
  // The avatar identity-colour seed for the quick-edit card (the starter id, or
  // "scratch") — kept stable while the name is edited; also the gallery's
  // selected-card signal.
  const [seedId, setSeedId] = useState<string | null>(null);
  const quickEditRef = useRef<HTMLDivElement>(null);
  // Synchronous single-flight guard for the (paid) drafter call — see generate().
  const generatingRef = useRef(false);

  // Picking a starter / "start from scratch" reveals the QUICK-EDIT card inline
  // (same screen, below the gallery) — no drafter call. The safety constraint is
  // re-asserted up front so it shows pinned. The full editor is one click away
  // (openFullEditor) and inherits this exact doc, so quick edits carry over.
  function openDirect(seedDoc: PersonaDoc, seed: string) {
    setDraft(null);
    setDoc(withDefaultModel(ensureSafetyConstraint(seedDoc), defaultModel));
    setSeedId(seed);
    setError(null);
  }

  function openStarter(example: PersonaExample) {
    openDirect(example.structure as unknown as PersonaDoc, example.id);
  }

  function openScratch() {
    openDirect(emptyPersonaDoc(), "scratch");
  }

  // Promote the current quick-edit draft into the full PersonaEditor. The doc
  // (with every quick edit) becomes the editor's initialDoc, so nothing is lost.
  function openFullEditor() {
    setEditorKey((k) => k + 1);
    setPhase("review");
  }

  // Create directly from the quick-edit draft (skips the full editor).
  async function createFromQuick() {
    if (!doc) return;
    const result = await handleCreate(docToYaml(doc));
    if (result?.error) setError(result.error);
  }

  // Bring the quick-edit card into view when a starter/scratch is first picked
  // (it renders below the gallery). Guarded for jsdom (no scrollIntoView).
  // biome-ignore lint/correctness/useExhaustiveDependencies: scroll on reveal only
  useEffect(() => {
    if (doc && phase === "describe") {
      quickEditRef.current?.scrollIntoView?.({
        behavior: "smooth",
        block: "start",
      });
    }
  }, [seedId, phase]);

  function applyDraft(next: AuthoringDraft): boolean {
    try {
      setDoc(withDefaultModel(yamlToDoc(next.yaml), defaultModel));
      setDraft(next);
      return true;
    } catch {
      setError(t("authorError"));
      return false;
    }
  }

  async function generate() {
    // Synchronous in-flight guard: a draft is a PAID, single-flight operation, so
    // a second generate while one is already running is a no-op. A ref (not the
    // `phase` STATE) is required — two same-tick calls (a double-click, or a
    // re-render/remount race) would both observe the pre-update `phase`, but the
    // ref is set synchronously at entry so the second call sees it. Cleared on
    // every exit (success / error / abort) via the finally.
    if (generatingRef.current) return;
    const desc = description.trim();
    if (!desc) return; // bail before claiming in-flight (no stream started)
    generatingRef.current = true;
    setPhase("loading");
    setError(null);
    setStreamText("");
    setStreamRetrying(false);
    try {
      // D-P0-raw-text-preview: paint the forming text live as it streams; on the
      // validation re-stream (D-P0-restream-retry) clear the preview so attempt 2
      // replaces attempt 1 rather than appending. Resolve to the review form on
      // the terminal draft.
      const result = await author(desc, {
        onChunk: (delta) => setStreamText((prev) => prev + delta),
        onRetry: () => {
          setStreamText("");
          setStreamRetrying(true);
        },
      });
      setRound(0);
      setEditorKey((k) => k + 1);
      if (applyDraft(result)) setPhase("review");
      else setPhase("describe");
    } catch (e) {
      // An aborted stream (unmount / supersede) is not a user-facing error.
      if (e instanceof Error && e.name === "AbortError") return;
      setError(
        e instanceof ApiError && e.isRateLimited
          ? t("rateLimited")
          : t("authorError"),
      );
      setPhase("describe");
    } finally {
      generatingRef.current = false;
    }
  }

  async function answerQuestion(
    question: string,
    answer: string,
    currentYaml: string,
  ) {
    if (refining || round >= MAX_REFINE_ROUNDS) return;
    setRefining(true);
    setError(null);
    try {
      const next = await refine({ currentYaml, question, answer, round });
      if (applyDraft(next)) {
        setRound((r) => r + 1);
        setEditorKey((k) => k + 1);
      }
    } catch (e) {
      setError(
        e instanceof ApiError && e.isRateLimited
          ? t("rateLimited")
          : t("authorError"),
      );
    } finally {
      setRefining(false);
    }
  }

  // The single direct-create assembly, shared by all three paths (Spec 36 T2):
  //   re-assert safety → validate against the v1 schema → POST.
  // Validation runs BEFORE the phase flips to "creating" so a failure leaves the
  // editor mounted (edits preserved) and surfaces inline via the editor's save
  // error. On success `createPersona` redirects; it returns only on a server
  // error, which drops back to the editor.
  async function handleCreate(
    yaml: string,
    _avatarUrl?: string | null,
  ): Promise<{ error: string } | undefined> {
    // Where to return on a SERVER error: the full editor lives in "review"; the
    // quick-edit card lives in "describe" (doc set). Capture before the flip.
    const origin: Phase = phase === "review" ? "review" : "describe";
    let parsed: PersonaDoc;
    try {
      parsed = yamlToDoc(yaml);
    } catch {
      return { error: t("authorError") };
    }
    const guarded = ensureSafetyConstraint(parsed);
    const validation = validatePersonaDoc(guarded);
    if (!validation.ok) {
      const fields = validation.issues.map((i) => i.path).join(", ");
      return { error: t("createValidationFailed", { fields }) };
    }
    setPhase("creating");
    setError(null);
    const result = await createPersona(docToYaml(guarded));
    if (result?.error) {
      setError(result.error);
      setPhase(origin);
    }
    return result;
  }

  if (phase === "review" && doc) {
    // Direct-create (starter / scratch) has no drafter draft → no refinement
    // seam and a "make it yours" framing instead of "review the draft".
    const direct = draft === null;
    return (
      <Stack gap={6} data-slot="author-wizard-review">
        <header>
          <p className="type-caption font-mono text-muted-foreground uppercase">
            {direct ? t("directByline") : t("reviewByline")}
          </p>
          <h1 className="type-heading mt-1" data-slot="author-wizard-title">
            {direct ? t("directTitle") : t("reviewTitle")}
          </h1>
          {direct ? (
            <p className="type-body mt-2 max-w-prose text-muted-foreground">
              {t("directSubtitle")}
            </p>
          ) : null}
        </header>
        {error ? (
          <p className="type-ui text-destructive" role="alert">
            {error}
          </p>
        ) : null}
        <PersonaEditor
          key={editorKey}
          initialDoc={doc}
          tools={tools}
          skills={skills}
          mcpServers={mcpServers}
          onSave={handleCreate}
          saveLabel={t("save")}
          refinement={
            draft
              ? {
                  questions: draft.questions ?? [],
                  round,
                  maxRounds: MAX_REFINE_ROUNDS,
                  refining,
                  onAnswer: (q, a, yaml) => void answerQuestion(q, a, yaml),
                }
              : undefined
          }
        />
      </Stack>
    );
  }

  if (phase === "loading") {
    return (
      <WizardLoading
        title={t("loadingTitle")}
        steps={[t("loadingStep1"), t("loadingStep2"), t("loadingStep3")]}
        streamText={streamText}
        retrying={streamRetrying}
        retryingLabel={t("streamRegenerating")}
      />
    );
  }

  if (phase === "creating") {
    // The structure already exists; now it's persisting + the avatar/voice
    // enrich asynchronously in the background (no taking-shape skeleton).
    return (
      <WizardLoading
        title={t("creatingTitle")}
        steps={[t("creatingStep1"), t("creatingStep2"), t("creatingStep3")]}
        skeleton={false}
      />
    );
  }

  return (
    <Stack gap={8} data-slot="author-wizard-describe">
      <header>
        <p className="type-caption font-mono text-muted-foreground uppercase">
          {t("describeByline")}
        </p>
        <h1 className="type-display mt-1" data-slot="author-wizard-title">
          {t("gallery.title")}
        </h1>
        <p className="type-body mt-2 max-w-prose text-muted-foreground">
          {t("gallery.subtitle")}
        </p>
      </header>

      {/* PRIMARY (top): describe your own → the LLM drafter, or start from
          scratch with an empty editable draft. */}
      <Stack gap={4} data-slot="author-wizard-own">
        <p className="type-body text-muted-foreground">{t("describeHint")}</p>

        {/* R9-025a: the mic sits inside the field, bottom-right — describe
            the persona by speaking instead of typing. */}
        <div className="relative">
          <Textarea
            ref={descriptionRef}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            rows={5}
            placeholder={t("describePlaceholder")}
            className="resize-none pr-10"
            data-slot="author-wizard-description"
          />
          <MicDictation
            value={description}
            onChange={setDescription}
            textareaRef={descriptionRef}
            language={dictationLanguage}
            className="absolute right-2 bottom-2"
          />
        </div>

        {/* Drafter-path errors only; quick-edit errors render in the card. */}
        {error && !doc ? (
          <p
            className="type-ui text-destructive"
            role="alert"
            data-slot="author-wizard-error"
          >
            {error}
          </p>
        ) : null}

        <div className="flex flex-wrap justify-end gap-3">
          <button
            type="button"
            onClick={openScratch}
            className={cn(buttonVariants({ variant: "outline" }), "gap-2")}
            data-slot="author-wizard-scratch"
          >
            <Sparkles className="size-4" aria-hidden="true" />
            {t("gallery.startScratch")}
          </button>
          <button
            type="button"
            onClick={() => void generate()}
            disabled={!description.trim()}
            className={cn(buttonVariants(), "gap-2")}
            data-slot="author-wizard-generate"
          >
            <Wand2 className="size-4" aria-hidden="true" />
            {t("generate")}
          </button>
        </div>
      </Stack>

      {/* SECONDARY (below): or pick a ready-made starter → edit → create
          directly (no drafter call). */}
      <div className="flex items-center gap-3" aria-hidden="true">
        <span className="h-px flex-1 bg-border" />
        <span className="type-caption font-mono text-muted-foreground uppercase">
          {t("gallery.ownPathLabel")}
        </span>
        <span className="h-px flex-1 bg-border" />
      </div>

      <ExampleGallery onSelect={openStarter} selectedId={seedId} />

      {/* QUICK-EDIT preview — appears when a starter/scratch is picked. Edit the
          essentials inline and create directly, or open the full editor (which
          inherits these exact edits). */}
      {doc ? (
        <div ref={quickEditRef}>
          <QuickEditCard
            doc={doc}
            seedId={seedId ?? "scratch"}
            onChange={setDoc}
            onCreate={() => void createFromQuick()}
            onOpenFullEditor={openFullEditor}
            creating={false}
            error={error}
          />
        </div>
      ) : null}
    </Stack>
  );
}

/**
 * Designed 10–30s loading state (spec §8 risk): the frontier call is slow, so
 * the surface reads as deliberate work — cycling status + persona-taking-shape
 * skeleton — not a blank spinner.
 *
 * Preserved behaviour (per audit.md §authoring.plumbing): the 3-step cycling
 * `setInterval` at 2800ms. T29 rebuild swaps shadcn `<Skeleton>` for T21
 * `<SkeletonLine>` (motion resolves through F1 `--motion-duration-*` tokens),
 * retokenises typography, and wraps in T20 `<Stack>`.
 */
function WizardLoading({
  title,
  steps,
  skeleton = true,
  streamText,
  retrying = false,
  retryingLabel,
}: {
  title: string;
  steps: string[];
  skeleton?: boolean;
  // Spec P0 — the raw draft text forming live (D-P0-raw-text-preview). When
  // non-empty it replaces the taking-shape skeleton with the streaming text.
  streamText?: string;
  // The validation-repair re-stream is in flight: surface a "regenerating"
  // status instead of the cycling step (D-P0-restream-retry).
  retrying?: boolean;
  retryingLabel?: string;
}) {
  const [i, setI] = useState(0);

  useEffect(() => {
    const id = setInterval(() => setI((n) => (n + 1) % steps.length), 2800);
    return () => clearInterval(id);
  }, [steps.length]);

  const hasStream = streamText !== undefined && streamText.length > 0;
  const statusLine = retrying && retryingLabel ? retryingLabel : steps[i];

  return (
    <Stack gap={6} data-slot="author-wizard-loading">
      <header className="flex items-center gap-3">
        <Wand2
          className="size-5 animate-pulse text-primary"
          aria-hidden="true"
        />
        <div>
          <h1 className="type-heading" data-slot="author-wizard-loading-title">
            {title}
          </h1>
          <p
            className="type-ui mt-1 text-muted-foreground"
            data-slot="author-wizard-loading-step"
            aria-live="polite"
          >
            {statusLine}
          </p>
        </div>
      </header>
      {hasStream ? (
        // The persona forming live: raw streamed text, NOT a parsed form, until
        // the terminal draft resolves to the review editor (D-P0-raw-text-preview).
        <Card className="p-5">
          <pre
            className="type-ui max-h-96 overflow-auto whitespace-pre-wrap break-words font-mono text-muted-foreground"
            data-slot="author-wizard-stream"
            aria-live="polite"
          >
            {streamText}
          </pre>
        </Card>
      ) : skeleton ? (
        <Stack gap={4}>
          {[0, 1, 2].map((row) => (
            <Card key={row} className="gap-3 p-5">
              <SkeletonLine className="w-24" />
              <SkeletonLine className="w-3/4" />
              <SkeletonLine className="w-1/2" />
            </Card>
          ))}
        </Stack>
      ) : null}
    </Stack>
  );
}
