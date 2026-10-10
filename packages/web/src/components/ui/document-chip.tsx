"use client";

import {
  Eye,
  FileCode,
  FileSpreadsheet,
  FileText,
  FileType,
  Table,
  X,
} from "lucide-react";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";
import { Card } from "@/components/ui/card";
import { cn } from "@/lib/utils";

/**
 * F3 — document chip component (T11).
 *
 * F3-local per D-F3-X-chip-placement; composes F2's `<Card>` + lucide
 * icons + token-based styling. Promotes to F2 only on F4/F5 second-
 * consumer reuse (strangler-fig).
 *
 * Renders a per-document chip: format icon + truncating filename +
 * size label + remove affordance. Scanned-PDF cue (T18) appears when
 * `strategy === "vision_handoff"` — the persona will "see" the pages
 * rather than "read" the text.
 */
export interface DocumentChipProps {
  /** Stable identifier within the conversation (mirrors `DocumentRef.doc_ref`). */
  docRef: string;
  filename: string;
  /** One of pdf | docx | xlsx | csv | txt | md | code (mirrors DocumentRef.format). */
  format: string;
  /** Original file size in bytes (mirrors DocumentRef.size_bytes). null when unknown. */
  sizeBytes: number | null;
  /** F3 T18: scanned-PDF cue when set. Mirrors DocumentRef.strategy. */
  strategy?: "whole_inject" | "retrieval" | "vision_handoff";
  /** D-FV0-31: the background index's state. Mirrors DocumentRef.index_state. */
  indexState?:
    | "not_needed"
    | "pending"
    | "indexing"
    | "ready"
    | "partial"
    | "failed";
  /** Only part of the text could be read. Mirrors DocumentRef.text_partial. */
  textPartial?: boolean;
  /** Remove this document from the conversation. */
  onRemove?: (docRef: string) => void;
  className?: string;
}

const FORMAT_ICONS: Record<string, ReactNode> = {
  pdf: <FileType className="size-4 shrink-0" aria-hidden />,
  docx: <FileText className="size-4 shrink-0" aria-hidden />,
  xlsx: <FileSpreadsheet className="size-4 shrink-0" aria-hidden />,
  csv: <Table className="size-4 shrink-0" aria-hidden />,
  txt: <FileText className="size-4 shrink-0" aria-hidden />,
  md: <FileText className="size-4 shrink-0" aria-hidden />,
  code: <FileCode className="size-4 shrink-0" aria-hidden />,
};

/**
 * D-FV0-31: the one state label a chip shows, or null for a document that is whole and
 * ready. The index state comes first; a cut text shows only when the index has nothing
 * to say. The persona is told the same, in a sentence, on every turn.
 */
export function documentStateKey(
  indexState: DocumentChipProps["indexState"],
  textPartial: boolean | undefined,
): "indexing" | "partial" | "failed" | "partlyRead" | null {
  if (indexState === "pending" || indexState === "indexing") return "indexing";
  if (indexState === "partial") return "partial";
  if (indexState === "failed") return "failed";
  if (textPartial) return "partlyRead";
  return null;
}

function formatSize(bytes: number | null): string {
  if (bytes === null) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function DocumentChip({
  docRef,
  filename,
  format,
  sizeBytes,
  strategy,
  indexState,
  textPartial,
  onRemove,
  className,
}: DocumentChipProps) {
  const t = useTranslations("chat.composer");
  const icon = FORMAT_ICONS[format] ?? FORMAT_ICONS.txt;
  const isScanned = strategy === "vision_handoff";
  const sizeLabel = formatSize(sizeBytes);
  const stateKey = documentStateKey(indexState, textPartial);

  return (
    <Card
      size="sm"
      // `flex-row` overrides the Card base's `flex-col` — without it the chip
      // stacks vertically and the remove X lands below the name instead of
      // inline at the end.
      className={cn(
        "flex w-fit max-w-xs flex-row items-center gap-2 px-3 py-2",
        className,
      )}
      data-slot="document-chip"
      data-format={format}
      data-strategy={strategy}
      data-index-state={indexState}
    >
      <span className="text-muted-foreground">{icon}</span>

      <div className="flex min-w-0 flex-1 flex-col">
        <span className="type-ui truncate text-foreground" title={filename}>
          {filename}
        </span>
        <span className="type-caption text-muted-foreground">
          {format.toUpperCase()}
          {sizeLabel ? ` · ${sizeLabel}` : ""}
          {stateKey ? (
            <span data-slot="document-state">
              {` · ${t(`documents.state.${stateKey}`)}`}
            </span>
          ) : null}
        </span>
      </div>

      {isScanned ? (
        <span
          className="text-muted-foreground"
          title={t("documents.scannedCue")}
          role="img"
          aria-label={t("documents.scannedCue")}
        >
          <Eye className="size-3.5" aria-hidden />
        </span>
      ) : null}

      {onRemove ? (
        <button
          type="button"
          onClick={() => onRemove(docRef)}
          aria-label={t("attach.remove")}
          className={cn(
            "grid size-6 shrink-0 place-items-center rounded-full",
            "text-muted-foreground hover:bg-muted hover:text-foreground",
            "focus-visible:outline-2 focus-visible:outline-offset-2",
            "focus-visible:outline-ring",
          )}
        >
          <X className="size-3" aria-hidden />
        </button>
      ) : null}
    </Card>
  );
}
