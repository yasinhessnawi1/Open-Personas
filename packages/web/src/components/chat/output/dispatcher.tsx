"use client";

import { useTranslations } from "next-intl";

import type { OutputContent } from "@/lib/api/output-content";
import { cn } from "@/lib/utils";

import { FileCard } from "../file-card";
import { DownloadChip } from "./download-chip";
import { InlineVisual } from "./inline-visual";
import { ResultBlock } from "./result-block";
import { WorkingState } from "./working-state";

/**
 * Spec F4 T09 — `<OutputDispatcher>`.
 *
 * The renderer router: reads ONE `OutputContent` discriminator and
 * dispatches to the right component (D-F4-X-presentation-hint-source).
 * No path/media-type inspection happens here — that policy belongs to
 * the normalisers (T03 chat / T04 run); by the time an `OutputContent`
 * reaches the dispatcher, the routing decision has already been made.
 * The dispatcher just maps `kind → component`.
 *
 * Six-variant exhaustiveness is enforced at compile time via the switch
 * exhaustiveness of `OutputContent['kind']`. Adding a new variant to
 * the union requires adding a matching branch here (TypeScript will
 * fail to type-check otherwise).
 *
 * Defensive layer — path-traversal:
 *   The backend resolves paths through `resolve_sandbox_path` and
 *   rejects traversal (`image_service.fetch:300`), so a malicious path
 *   already can't escape the workspace. But for honesty at the UI
 *   boundary, paths containing `..` are clamped: the dispatcher renders
 *   the corresponding failure variant rather than risking a misleading
 *   download / src attribute. Layer-2 defence, not the primary one.
 *
 * `<OutputList>` is the sibling helper for callers that have an
 * `OutputContent[]` (the typical case after the normaliser fires).
 * Pure presentation; consumers (T10 message-element.tsx / T11
 * step-card.tsx) handle stream-order placement.
 */
export interface OutputDispatcherProps {
  personaId: string;
  output: OutputContent;
  /** Forwarded to `<InlineVisual>` so the lightbox handler (T12) can open. */
  onViewLarger?: (workspacePath: string) => void;
  className?: string;
}

function hasPathTraversal(p: string): boolean {
  // `..` segments at any position (windows + posix). Rejects `../`, `..\\`,
  // and a bare `..` segment. Encoded variants would already have been
  // rejected by the backend resolver — the UI layer is defence-in-depth.
  return p.split(/[\\/]/).some((seg) => seg === "..");
}

export function OutputDispatcher({
  personaId,
  output,
  onViewLarger,
  className,
}: OutputDispatcherProps) {
  const t = useTranslations("chat.output");
  switch (output.kind) {
    case "inline-image": {
      if (hasPathTraversal(output.workspace_path)) {
        return (
          <FailureCard
            operation="render"
            error_message={t("invalidPath")}
            className={className}
          />
        );
      }
      return (
        <InlineVisual
          personaId={personaId}
          workspacePath={output.workspace_path}
          mediaType={output.media_type}
          intent="image"
          alt={output.alt}
          caption={output.caption}
          onViewLarger={
            onViewLarger !== undefined
              ? () => onViewLarger(output.workspace_path)
              : undefined
          }
          className={className}
        />
      );
    }

    case "inline-chart": {
      if (hasPathTraversal(output.workspace_path)) {
        return (
          <FailureCard
            operation="render"
            error_message={t("invalidPath")}
            className={className}
          />
        );
      }
      // Charts don't carry an `alt` on the wire — derive a defensible one
      // from the filename so screen readers announce something useful.
      const filename = output.workspace_path.split("/").pop() ?? "chart";
      return (
        <InlineVisual
          personaId={personaId}
          workspacePath={output.workspace_path}
          mediaType={output.media_type}
          intent="chart"
          alt={filename}
          prose_context={output.prose_context}
          onViewLarger={
            onViewLarger !== undefined
              ? () => onViewLarger(output.workspace_path)
              : undefined
          }
          className={className}
        />
      );
    }

    case "download-doc": {
      if (hasPathTraversal(output.workspace_path)) {
        return (
          <FailureCard
            operation="render"
            error_message={t("invalidPath")}
            className={className}
          />
        );
      }
      return (
        <DownloadChip
          personaId={personaId}
          workspacePath={output.workspace_path}
          mediaType={output.media_type}
          name={output.name}
          sizeBytes={output.size_bytes}
          className={className}
        />
      );
    }

    case "file-card": {
      if (hasPathTraversal(output.workspace_path)) {
        return (
          <FailureCard
            operation="render"
            error_message={t("invalidPath")}
            className={className}
          />
        );
      }
      return (
        <FileCard
          personaId={personaId}
          workspacePath={output.workspace_path}
          mediaType={output.media_type}
          name={output.name}
          sizeBytes={output.size_bytes}
          renderedInline={output.rendered_inline}
          className={className}
        />
      );
    }

    case "result-block":
      return (
        <ResultBlock
          stdout={output.stdout}
          truncated={output.truncated}
          code={output.code}
          language={output.language}
          className={className}
        />
      );

    case "working":
      return (
        <WorkingState
          operation={output.operation}
          label={output.label}
          className={className}
        />
      );

    case "failure":
      return (
        <FailureCard
          operation={output.operation}
          error_message={output.error_message}
          className={className}
        />
      );
  }
}

/**
 * Inline failure card for the `failure` variant — minimal F2-voiced
 * surface. A dedicated `<ErrorState>` already exists in F2; this is a
 * lighter, inline-flow variant for per-tool / per-step failures (the
 * tool-card path already shows raw error content; this surfaces the
 * operation prominently for the dispatcher's consumer surfaces).
 */
function FailureCard({
  operation,
  error_message,
  className,
}: {
  operation: string;
  error_message: string;
  className?: string;
}) {
  return (
    <div
      role="alert"
      className={cn(
        "rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2",
        "type-ui text-destructive",
        className,
      )}
      data-slot="output-failure"
      data-operation={operation}
    >
      <span className="font-mono font-medium">{operation}</span>
      <span className="ml-2">{error_message}</span>
    </div>
  );
}

/**
 * Sibling helper — renders an `OutputContent[]` as a vertical stack of
 * dispatched components. Callers that have a derived list (chat
 * normaliser output, `step.outputs`) consume this; callers that
 * interleave per-event consume `<OutputDispatcher>` directly.
 */
export interface OutputListProps {
  personaId: string;
  outputs: ReadonlyArray<OutputContent>;
  onViewLarger?: (workspacePath: string) => void;
  className?: string;
}

export function OutputList({
  personaId,
  outputs,
  onViewLarger,
  className,
}: OutputListProps) {
  if (outputs.length === 0) return null;
  return (
    <div
      className={cn("flex flex-col gap-2", className)}
      data-slot="output-list"
    >
      {outputs.map((output, i) => (
        <OutputDispatcher
          // Outputs come from an ordered event stream; index keying is
          // stable for the lifetime of a given render pass. The
          // accumulator (T10/T11) replaces the array wholesale on each
          // event, so cross-render reuse isn't a concern here.
          // biome-ignore lint/suspicious/noArrayIndexKey: stream-ordered, replaced wholesale
          key={i}
          personaId={personaId}
          output={output}
          onViewLarger={onViewLarger}
        />
      ))}
    </div>
  );
}
