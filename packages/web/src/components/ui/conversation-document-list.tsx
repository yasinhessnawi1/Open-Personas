"use client";

import { useTranslations } from "next-intl";
import type { DocumentRef } from "@/lib/upload";
import { cn } from "@/lib/utils";
import { DocumentChip } from "./document-chip";

/**
 * F3 (T12) — conversation document list panel.
 *
 * Renders the active conversation's attached documents as a flex-wrap row
 * of `<DocumentChip>` instances. Empty state surfaces the "no documents
 * attached yet" copy via F2's voice. Per-chip remove fires `onRemove`
 * which the parent wires to T14's delete flow.
 *
 * Inline layout for v0.1 (sits above the composer textarea); F2's
 * `<Sheet>` mobile variant is the v0.2 enhancement when the document
 * count grows (T21 verifies the 375px viewport).
 */
export interface ConversationDocumentListProps {
  documents: DocumentRef[];
  /** Called when the user removes a document via the chip's X affordance. */
  onRemove: (docRef: string) => void;
  className?: string;
}

export function ConversationDocumentList({
  documents,
  onRemove,
  className,
}: ConversationDocumentListProps) {
  const t = useTranslations("chat.composer.documents");

  if (documents.length === 0) {
    return null; // The composer keeps a clean surface when no docs attached.
  }

  return (
    <section
      aria-label={t("panelTitle")}
      className={cn("flex flex-wrap gap-2", className)}
      data-slot="conversation-document-list"
      data-count={documents.length}
    >
      {documents.map((doc) => (
        <DocumentChip
          key={doc.doc_ref}
          docRef={doc.doc_ref}
          filename={doc.filename}
          format={doc.format}
          sizeBytes={doc.size_bytes ?? null}
          strategy={
            // DocumentChip narrows to the 3-public-strategy union (the
            // `_REQUIRED` variant never persists on a DocumentRef per
            // exploration.md X-F3-1 — document_service rasterises before
            // writing the sidecar).
            doc.strategy as "whole_inject" | "retrieval" | "vision_handoff"
          }
          indexState={doc.index_state}
          textPartial={doc.text_partial}
          onRemove={onRemove}
        />
      ))}
    </section>
  );
}
