"""The ``file_extract`` job handler — Turn-into-file (R9-025b).

Owner's words: "takes the output of persona and turns it into a file, getting
some more context from episodic, stripping out persona reply filler and
extracting what can be turned into a file — might be a pdf, or a table so
maybe excel — a shortcut replacing the ritual 'now draft that as a PDF' turn."

A message action → this durable A0 tenant: re-reads the target assistant
message + a bounded conversation window + bounded episodic recall, asks an
LLM to strip conversational filler and extract the SUBSTANTIVE content (+
decide the output format when the caller asked for ``"auto"``), and produces
the file via the SAME ``document_generation`` sandbox machinery the live chat
path uses — never hand-rolled pdf/xlsx plumbing.

**Episodic-context decision.** Reuses :class:`persona.stores.episodic.EpisodicStore`'s
``query`` method directly (:func:`build_file_extract_episodic_query`) — the SAME
store/method the chat path's K9 unified-recall closure
(``RuntimeFactory._build_unified_recall``'s ``episodic_query``) and the
origination adapters construct identically. This is deliberately NOT the full
``persona_runtime.retrieval.retrieve_context`` turn-assembly pipeline (self-facts
+ worldview + graph + core-block + unified-recall gating + CPU reranker) — that
pipeline is scoped to live conversational turns and composing it here would need
substantial additional worker-root wiring (a graph store, a core-block provider)
disproportionate to "a little more context" for a one-off extraction. Gated on
``memory_backend is not None`` (the graceful-degrade boundary ``worker_root.py``
already uses everywhere else) — absent, episodic context is simply empty: a
conversation-window-only v1, never a failure.

**Doc-gen sandbox-boundary decision.** ``document_generation`` is an
instruction-DISPATCH skill (Reading B, D-24-1 —
:mod:`persona.skills.document_generation`): a "handler" names a format + the
sandbox library its ``SKILL.md`` teaches, and the BYTES are produced by code
that RUNS IN the ``code_execution`` sandbox — persona-core/persona-api take
ZERO rendering dependencies (no ``reportlab``/``openpyxl`` import anywhere in
this module). This handler therefore does not hand-roll pdf/xlsx plumbing: it
drives the SAME sandbox boundary the live ``code_execution`` tool drives
(:class:`persona.sandbox.protocol.CodeSandbox`, via the app's own
:class:`persona_api.sandbox.pool.SandboxPool` — one substrate, shared with the
live chat path), executing a small DETERMINISTIC Python snippet (built by this
module, not model-authored — a background job has no chat loop to catch and
retry a broken generation turn-by-turn) that calls the exact pre-installed
libraries the ``document_generation`` ``SKILL.md`` teaches per format
(``reportlab``→``matplotlib`` try-import fallback for pdf, ``openpyxl`` for
xlsx, stdlib for md/csv) and writes to ``/workspace/out/<name><ext>`` — the
documented produced-file convention both the local-Docker and E2B-hosted
substrates auto-discover. Produced bytes are copied out and F5-sidecar-tagged
exactly like the live ``code_execution`` tool's own produced-file persister
(:func:`persona_api.sandbox.runtime_tool.make_pool_code_execution_tool`'s
``_persist_produced_file`` closure — Path-based, under ``workspace_root``, NOT
routed through the ``FileStorage``/S3 seam, matching that exact precedent):
pdf/xlsx tag ``producing_spec="16"`` (Spec 16 doc generation — the SAME value
``_classify_for_sidecar`` assigns docx/pptx/xlsx/pdf produced files today);
csv/md tag ``"12"`` (Spec 12 general produced file — the same fallback bucket
``_classify_for_sidecar`` gives non-doc bare refs). A Turn-into-file artifact is
therefore indistinguishable from one the persona produced mid-chat via the same
skill. ``csv`` has no dedicated ``document_generation`` format handler (the
registry's curated six are docx/pdf/pptx/xlsx/md/txt) — it is rendered the same
way md/txt already are (``library="stdlib"``, no new rendering dependency),
consistent with the registry's own stdlib tier, not a hand-rolled binary format.

A DEDICATED, isolated sandbox session is acquired per run
(``pool.acquire(user_id=owner_id, conversation_id=f"file-extract-{message_id}")``
— never the live chat conversation's own session id), so a Turn-into-file job
never shares interpreter state with (or races) an in-progress interactive turn;
the session is released immediately after the one execute()+copy() round-trip.

**Refresh-signal decision.** Publishes ``sidebar.changed``
(reason=``"conversation.file_extracted"``) via the SAME best-effort
``publish_sidebar_changed`` helper ``title_refresh`` uses — the one
general-purpose "something changed, refetch" live channel already wired
end-to-end (SSE transport, ``MeEventsProvider``, dedup, resume). Stated
honestly: R9-024's ``ConversationFiles`` panel does NOT currently subscribe to
``sidebar.changed`` — its own two refresh triggers are same-tab-only client
``CustomEvent``s (``CONVERSATION_FILES_CHANGED_EVENT`` / ``CHAT_STREAMING_EVENT``)
plus a refetch-on-open effect, and a background job cannot fire a same-tab
``CustomEvent``. Today the Files panel's floor for THIS feature is therefore
"refresh on next open" (an already-shipped pull path) — publishing
``sidebar.changed`` anyway is zero-risk, reuses proven infra end-to-end, and
primes an easy follow-up (subscribing ``ConversationFiles`` to it) without this
batch reaching into R9-024's component.

**Failure posture.** An LLM extraction failure or a sandbox/render failure
raises :class:`persona.errors.FileExtractionError` (a fixed-vocabulary
``context={"reason": ...}``, NEVER a partial file — nothing is persisted before
the render fully succeeds) and is retried per the job system's PLAIN default
:class:`~persona.jobs.RetryPolicy` (no override — an LLM/sandbox hiccup is an
ordinary transient background failure, exactly like any other). The handler
only ever READS conversation/message rows — it never blocks or affects the
conversation.
"""

from __future__ import annotations

import html
import json
import re
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from persona.errors import FileExtractionError
from persona.jobs import MEDIUM_LEASE, JobPayload, JobTypeSpec, RetryPolicy
from persona.logging import get_logger
from persona.sandbox.errors import SandboxError
from persona.sandbox.result import NetworkPolicy, ResourceLimits
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from persona_api.db.models import conversations, messages
from persona_api.services.artifact_metadata import (
    WorkspaceArtifactMetadata,
    utcnow,
    write_artifact_sidecar,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    from pathlib import Path

    from persona.audit import AuditLogger
    from persona.backends import ChatBackend
    from persona.jobs import JobContext, JobRegistry
    from persona.schema.conversation import ConversationMessage
    from persona.stores.backend import Backend
    from sqlalchemy import Connection

    from persona_api.jobs.queue import JobQueue, JobRecord
    from persona_api.realtime.channel import UserEventChannel
    from persona_api.sandbox.pool import SandboxPool

__all__ = [
    "FILE_EXTRACT_JOB_TYPE",
    "RENDER_FORMATS",
    "REQUEST_FORMATS",
    "ExtractedContent",
    "FileExtractContext",
    "FileExtractHandler",
    "FileExtractJobPayload",
    "FileExtractRenderer",
    "FileExtractRepository",
    "PgFileExtractRepository",
    "RenderedFile",
    "SandboxFileRenderer",
    "build_extraction_prompt",
    "build_file_extract_episodic_query",
    "build_file_extract_generator",
    "conversation_window_excerpt",
    "decide_format",
    "enqueue_file_extract",
    "episodic_context_excerpt",
    "file_extract_idempotency_key",
    "file_extract_queue_ready",
    "parse_extracted_content",
    "register_file_extract_handler",
    "render_sandbox_code",
]

_logger = get_logger("jobs.file_extract")

FILE_EXTRACT_JOB_TYPE = "file_extract"

#: The four renderable output formats (v1 scope). docx/pptx are NOT offered —
#: the owner's ask is pdf/table(xlsx); md/csv round out "prose vs tabular"
#: without adding a rendering dependency (both are stdlib-tier, see module
#: docstring). Mirrors the doc-gen registry's format-key vocabulary shape.
RENDER_FORMATS: tuple[str, ...] = ("pdf", "md", "xlsx", "csv")
#: What the route/payload accept — RENDER_FORMATS plus the calm default.
REQUEST_FORMATS: tuple[str, ...] = ("auto", *RENDER_FORMATS)

# Roles whose text is real conversational content (mirrors title_refresh's
# ``_TRANSCRIPT_ROLES`` discipline — system nudges / tool frames never enter
# the extraction window).
_TRANSCRIPT_ROLES = ("user", "assistant")

# Bounded conversation window (R9-025b: "last ~20 messages, per-message
# truncation"). A larger per-message cap than title_refresh's 500 — extraction
# needs the actual substance, not just a titling gist.
_WINDOW_MESSAGES = 20
_WINDOW_MESSAGE_CHARS = 1200

# Bounded episodic recall (mirrors document_service.build_document_context's
# retrieval_top_k=5 default).
_EPISODIC_TOP_K = 5
_EPISODIC_SNIPPET_CHARS = 400

_OUT_NAME_FALLBACK = "document"


class FileExtractJobPayload(JobPayload):
    """Which message to extract + the requested output format.

    ``format`` is kept as a plain ``str`` (not a ``Literal``) — the closed
    ``REQUEST_FORMATS`` vocabulary is enforced at the route boundary (a
    Pydantic ``Literal`` on the request body); a loose payload field means a
    future format addition never needs a stored-JSONB payload migration.
    """

    conversation_id: str
    message_id: str
    format: str


def file_extract_idempotency_key(payload: FileExtractJobPayload) -> str:
    """``file_extract:{message_id}:{format}`` — one extraction per message+format."""
    return f"file_extract:{payload.message_id}:{payload.format}"


def file_extract_queue_ready(*, tier_registry: object | None, sandbox_pool: object | None) -> bool:
    """THE gate shared by the route (producer) and worker registration (R9-013 precedent).

    Mirrors ``avatar_queue_ready`` (``jobs/handlers/avatar.py``): a
    ``file_extract`` job may be enqueued **iff the worker will have a handler
    for it** — a model backend AND a sandbox pool are both composed. A
    producer-only read of "is this configured" is exactly the half-shipped
    cutover that poison-loops jobs into a handler-less worker.
    """
    return tier_registry is not None and sandbox_pool is not None


# ----- durable re-read: target message + bounded window (job-connection) -----


class FileExtractContext(BaseModel):
    """What the repository re-reads durably: the target + its persona + a bounded window."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    persona_id: str
    target_role: str
    target_content: str
    window: tuple[tuple[str, str], ...]  # (role, content), CHRONOLOGICAL order


@runtime_checkable
class FileExtractRepository(Protocol):
    """The DB port the handler uses (owner-scoped via the job's connection)."""

    def read(
        self, conn: Connection, *, conversation_id: str, message_id: str
    ) -> FileExtractContext | None:
        """Read the target message + its bounded window, or ``None`` if either is gone."""
        ...


class PgFileExtractRepository:
    """Postgres-backed :class:`FileExtractRepository` (owner-scoped via the job conn)."""

    def read(
        self, conn: Connection, *, conversation_id: str, message_id: str
    ) -> FileExtractContext | None:
        conv = conn.execute(
            select(conversations.c.persona_id).where(conversations.c.id == conversation_id)
        ).one_or_none()
        if conv is None:
            return None
        target = conn.execute(
            select(messages.c.role, messages.c.content).where(
                messages.c.id == message_id,
                messages.c.conversation_id == conversation_id,
            )
        ).one_or_none()
        if target is None:
            return None
        # Last ~20 by recency, then restored to chronological order in Python —
        # `created_at` carries a per-row microsecond offset at write time
        # (chat_turn_sink), so this ordering is stable (mirrors chat_service's
        # own plain `.order_by(created_at)` — no tiebreak needed).
        rows = conn.execute(
            select(messages.c.role, messages.c.content)
            .where(messages.c.conversation_id == conversation_id)
            .order_by(messages.c.created_at.desc())
            .limit(_WINDOW_MESSAGES)
        ).all()
        window = tuple(
            (r.role, r.content) for r in reversed(rows) if r.role in _TRANSCRIPT_ROLES and r.content
        )
        return FileExtractContext(
            persona_id=conv.persona_id,
            target_role=target.role,
            target_content=target.content,
            window=window,
        )


# ----- bounded excerpting (pure, unit-testable) ------------------------------


def _truncate(text: str, *, cap: int) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= cap:
        return collapsed
    return collapsed[:cap] + "…"


def conversation_window_excerpt(window: Sequence[tuple[str, str]]) -> str:
    """Render the bounded window as ``role: text`` lines, each per-message-capped."""
    lines = [f"{role}: {_truncate(content, cap=_WINDOW_MESSAGE_CHARS)}" for role, content in window]
    return "\n".join(lines)


def episodic_context_excerpt(snippets: Sequence[str]) -> str:
    """Render bounded episodic-recall snippets as a bullet list (empty ⇒ empty string)."""
    if not snippets:
        return ""
    lines = [f"- {_truncate(s, cap=_EPISODIC_SNIPPET_CHARS)}" for s in snippets]
    return "\n".join(lines)


# ----- format decision (pure, unit-testable — the auto heuristic matrix) ----


def decide_format(requested: str, kind: Literal["prose", "tabular"]) -> str:
    """The format-decision matrix (R9-025b).

    - An EXPLICIT request (pdf/md/xlsx/csv) is honoured verbatim regardless of
      the extracted content's natural shape — the user asked for a specific
      format; the renderer's tabular/prose fallback (see ``_tabular_rows`` /
      ``_paragraphs``) covers a shape mismatch.
    - ``"auto"`` (the calm default): tabular content → ``xlsx`` (the owner's
      "might be... a table so maybe excel" pick — richer than raw csv); prose
      content → ``pdf`` (the exact ritual this feature replaces — "now draft
      that as a PDF").
    """
    if requested != "auto":
        return requested
    return "xlsx" if kind == "tabular" else "pdf"


# ----- LLM extraction: prompt assembly + structured-output parsing ----------

_EXTRACTION_SYSTEM_PROMPT = (
    "You turn one chat message into the SUBSTANCE of a standalone document. "
    'Strip conversational filler (greetings, hedges, "let me know if...", '
    "meta-commentary about the reply itself) and keep only what the user "
    "would actually want IN the file. Use the conversation window and any "
    "recalled memory ONLY as context to understand references — the target "
    "message is what gets turned into the document.\n\n"
    "Decide the content's natural shape:\n"
    '- "tabular": the substance is fundamentally a table (comparison, list of '
    "items with attributes, structured data) — provide columns + rows.\n"
    '- "prose": the substance is a written passage (explanation, report, '
    "narrative, list of prose points) — provide body_markdown.\n\n"
    "Output ONLY a single JSON object, no prose, no markdown code fence:\n"
    '{"kind": "prose"|"tabular", "title": "<short descriptive title>", '
    '"body_markdown": "<the substance as markdown - required, even for '
    'tabular give a short caption>", "columns": ["<col>", ...], '
    '"rows": [["<cell>", ...], ...]}\n'
    'columns/rows may be empty arrays when kind is "prose".'
)


def build_extraction_prompt(
    *,
    target_content: str,
    window_excerpt: str,
    episodic_excerpt: str,
) -> list[ConversationMessage]:
    """Assemble the (system, user) extraction prompt from BOUNDED inputs only."""
    from datetime import UTC, datetime  # noqa: PLC0415

    from persona.schema.conversation import ConversationMessage  # noqa: PLC0415

    now = datetime.now(UTC)
    sections = [f"TARGET MESSAGE (turn this into a file):\n{target_content}"]
    if window_excerpt:
        sections.append(f"CONVERSATION WINDOW (context only):\n{window_excerpt}")
    if episodic_excerpt:
        sections.append(f"RECALLED MEMORY (context only):\n{episodic_excerpt}")
    return [
        ConversationMessage(role="system", content=_EXTRACTION_SYSTEM_PROMPT, created_at=now),
        ConversationMessage(role="user", content="\n\n".join(sections), created_at=now),
    ]


class ExtractedContent(BaseModel):
    """The LLM extraction's structured output — substance, conversational filler stripped."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    kind: Literal["prose", "tabular"]
    body_markdown: str = Field(min_length=1)
    columns: tuple[str, ...] = ()
    rows: tuple[tuple[str, ...], ...] = ()


def _strip_code_fence(text: str) -> str:
    """Strip a leading/trailing markdown code fence (```json ... ```), if present.

    Mirrors ``persona_runtime.extraction.parse``'s lenient JSON-mode discipline
    (no provider ``response_format`` dependency — the model returns JSON text
    and this parses it defensively).
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    body = stripped.split("\n", 1)[1] if "\n" in stripped else ""
    if body.rstrip().endswith("```"):
        body = body.rstrip()[: -len("```")]
    return body.strip()


def _stringify_table(parsed: dict[str, Any]) -> dict[str, Any]:
    """Best-effort coercion of columns/rows cells to ``str``.

    LLM-produced tables often mix types (a numeric cell, a null) — coerce
    defensively so ONE non-string cell doesn't sink an otherwise-usable
    extraction (Pydantic v2 does not implicitly coerce int/float to ``str``).
    """
    out = dict(parsed)
    if isinstance(out.get("columns"), list):
        out["columns"] = [str(c) for c in out["columns"]]
    if isinstance(out.get("rows"), list):
        out["rows"] = [
            [str(cell) for cell in row] if isinstance(row, list) else row for row in out["rows"]
        ]
    return out


def parse_extracted_content(raw: str) -> ExtractedContent | None:
    """Lenient JSON → :class:`ExtractedContent`; unusable output → ``None``.

    Never raises (mirrors the K2 extraction parser's lenient discipline) — a
    malformed/empty generation is a job FAILURE the caller raises explicitly
    (:class:`~persona.errors.FileExtractionError`, reason
    ``"llm_extraction_failed"``), not a silent partial.
    """
    stripped = _strip_code_fence(raw)
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    try:
        return ExtractedContent.model_validate(_stringify_table(parsed))
    except Exception:  # noqa: BLE001 — any validation shape mismatch → unusable
        return None


def build_file_extract_generator(
    backend: ChatBackend,
) -> Callable[[list[ConversationMessage]], Awaitable[ExtractedContent | None]]:
    """Close the composed FILE-EXTRACT-tier backend over prompt → :class:`ExtractedContent`.

    The backend is resolved ONCE at worker composition (``worker_root``), the
    exact ``title_refresh``/``synthesis`` build-time pattern — never per job.
    """

    async def generate(prompt: list[ConversationMessage]) -> ExtractedContent | None:
        response = await backend.chat(prompt, temperature=0.0, max_tokens=2048)
        return parse_extracted_content(response.content or "")

    return generate


def build_file_extract_episodic_query(
    memory_backend: Backend, audit_logger: AuditLogger | None
) -> Callable[[str, str], tuple[str, ...]]:
    """Close an owner-scoped :class:`EpisodicStore` over ``(persona_id, text) -> snippets``.

    Reuses ``EpisodicStore.query`` — the SAME store/method the chat path's
    recall closures construct (see the module decisions above). Runs inside
    the job's per-job owner-scoped context, so a Postgres-backed store's reads
    are automatically owner-scoped (the worker's choke point already set
    ``current_user_id`` before the handler runs).
    """
    from persona.stores.episodic import EpisodicStore  # noqa: PLC0415

    store = EpisodicStore(backend=memory_backend, audit_logger=audit_logger)

    def query(persona_id: str, text: str) -> tuple[str, ...]:
        chunks = store.query(persona_id, text, _EPISODIC_TOP_K)
        return tuple(c.text for c in chunks)

    return query


# ----- sandbox code rendering (pure, unit-testable — no rendering deps here) -


_SLUG_UNSAFE_RE = re.compile(r"[^a-z0-9]+")


def _slugify(title: str) -> str:
    """Lowercase, hyphenated, ASCII-safe filename stem (the SKILL.md's own convention)."""
    lowered = title.strip().lower()
    slug = _SLUG_UNSAFE_RE.sub("-", lowered).strip("-")
    return slug[:60]


def _paragraphs(body_markdown: str) -> list[str]:
    parts = re.split(r"\n\s*\n", body_markdown.strip())
    return [p.strip() for p in parts if p.strip()]


def _tabular_rows(content: ExtractedContent) -> tuple[tuple[str, ...], list[tuple[str, ...]]]:
    """Return ``(columns, rows)`` — the real table, or a one-column paragraph fallback.

    Covers an explicit xlsx/csv request against PROSE content (no natural
    table): degrade honestly to a single "Content" column, one paragraph per
    row — never a hard failure just because the shapes don't naturally match.
    """
    if content.columns and content.rows:
        return content.columns, list(content.rows)
    paragraphs = _paragraphs(content.body_markdown) or (content.body_markdown,)
    return ("Content",), [(p,) for p in paragraphs]


def _escape_for_reportlab(text: str) -> str:
    """Escape reportlab's mini-XML markup chars (``Paragraph`` parses a tiny XML subset)."""
    return html.escape(text, quote=False)


def _render_md_code(content: ExtractedContent, out_name: str) -> str:
    body = f"# {content.title}\n\n{content.body_markdown}\n"
    return (
        "from pathlib import Path\n"
        f"Path('/workspace/out/{out_name}.md').write_text({body!r}, encoding='utf-8')\n"
    )


def _render_csv_code(content: ExtractedContent, out_name: str) -> str:
    columns, rows = _tabular_rows(content)
    return (
        "import csv\n"
        f"with open('/workspace/out/{out_name}.csv', 'w', newline='', encoding='utf-8') as f:\n"
        "    writer = csv.writer(f)\n"
        f"    writer.writerow({list(columns)!r})\n"
        f"    for row in {rows!r}:\n"
        "        writer.writerow(row)\n"
    )


def _render_xlsx_code(content: ExtractedContent, out_name: str) -> str:
    columns, rows = _tabular_rows(content)
    return (
        "from openpyxl import Workbook\n"
        "wb = Workbook(); ws = wb.active\n"
        f"ws.append({list(columns)!r})\n"
        f"for row in {rows!r}:\n"
        "    ws.append(row)\n"
        f"wb.save('/workspace/out/{out_name}.xlsx')\n"
    )


def _render_pdf_code(content: ExtractedContent, out_name: str) -> str:
    # Mirrors the document_generation SKILL.md's own pdf sample VERBATIM in
    # structure: try reportlab (rich flowables) first, degrade to matplotlib
    # PdfPages on ModuleNotFoundError (the offline-template floor) — the ONLY
    # place doc-gen branches on a library, at import time, no network.
    title_rl = _escape_for_reportlab(content.title)
    plain_paragraphs = _paragraphs(content.body_markdown) or [""]
    escaped_paragraphs = [_escape_for_reportlab(p) for p in plain_paragraphs]
    return (
        "try:\n"
        "    from reportlab.lib.pagesizes import A4\n"
        "    from reportlab.lib.styles import getSampleStyleSheet\n"
        "    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer\n"
        "    styles = getSampleStyleSheet()\n"
        f"    doc = SimpleDocTemplate('/workspace/out/{out_name}.pdf', pagesize=A4)\n"
        f"    story = [Paragraph({title_rl!r}, styles['Title']), Spacer(1, 12)]\n"
        f"    for para in {escaped_paragraphs!r}:\n"
        "        story.append(Paragraph(para, styles['BodyText']))\n"
        "        story.append(Spacer(1, 8))\n"
        "    doc.build(story)\n"
        "except ModuleNotFoundError:\n"
        "    import matplotlib; matplotlib.use('Agg')\n"
        "    import matplotlib.pyplot as plt\n"
        "    from matplotlib.backends.backend_pdf import PdfPages\n"
        "    from textwrap import wrap\n"
        f"    with PdfPages('/workspace/out/{out_name}.pdf') as pdf:\n"
        "        fig = plt.figure(figsize=(8.27, 11.69))\n"
        f"        fig.text(0.08, 0.95, {content.title!r}, fontsize=18, weight='bold', va='top')\n"
        "        y = 0.88\n"
        f"        for para in {plain_paragraphs!r}:\n"
        "            for line in wrap(para, 90):\n"
        "                fig.text(0.08, y, line, fontsize=11, va='top'); y -= 0.025\n"
        "                if y < 0.08:\n"
        "                    plt.axis('off'); pdf.savefig(fig); plt.close(fig)\n"
        "                    fig = plt.figure(figsize=(8.27, 11.69)); y = 0.95\n"
        "            y -= 0.02\n"
        "        plt.axis('off'); pdf.savefig(fig); plt.close(fig)\n"
    )


def render_sandbox_code(content: ExtractedContent, format: str, out_name: str) -> str:  # noqa: A002
    """Deterministically build the sandbox Python snippet for ``format``.

    Mirrors the ``document_generation`` ``SKILL.md``'s own per-format code
    samples (pdf's reportlab→matplotlib try-import fallback; openpyxl for
    xlsx; stdlib writes for md/csv) — the SAME libraries the live persona is
    taught to use when it drives this skill mid-chat, just assembled
    deterministically here instead of model-authored.
    """
    if format == "md":
        return _render_md_code(content, out_name)
    if format == "csv":
        return _render_csv_code(content, out_name)
    if format == "xlsx":
        return _render_xlsx_code(content, out_name)
    if format == "pdf":
        return _render_pdf_code(content, out_name)
    msg = f"file_extract: no sandbox renderer for format {format!r}"
    raise ValueError(msg)


# ----- the doc-gen sandbox boundary (the renderer) --------------------------

_EXT_BY_FORMAT: dict[str, str] = {"pdf": ".pdf", "md": ".md", "xlsx": ".xlsx", "csv": ".csv"}
_MEDIA_TYPE_BY_FORMAT: dict[str, str] = {
    "pdf": "application/pdf",
    "md": "text/markdown",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "csv": "text/csv",
}
# Mirrors runtime_tool.py's `_classify_for_sidecar` doc/data split: docx/pptx/
# xlsx/pdf -> ("doc", "16"); csv/parquet/json -> ("data", "12"); md falls into
# the same "12" general-produced-file bucket _classify_for_sidecar uses for
# any bare ref outside its curated doc/data extension sets.
_PRODUCING_SPEC_BY_FORMAT: dict[str, str] = {"pdf": "16", "xlsx": "16", "csv": "12", "md": "12"}
_SIDECAR_TYPE_BY_FORMAT: dict[str, str] = {"pdf": "doc", "xlsx": "doc", "csv": "data", "md": "doc"}


class RenderedFile(BaseModel):
    """The output of one sandbox render — a workspace-relative path + descriptor."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace_path: str  # e.g. "uploads/<name>.<ext>"
    media_type: str
    size_bytes: int = Field(ge=0)
    format: str


@runtime_checkable
class FileExtractRenderer(Protocol):
    """Renders extracted content to a real, persisted file via the doc-gen sandbox boundary."""

    async def render(
        self,
        content: ExtractedContent,
        *,
        format: str,  # noqa: A002
        owner_id: str,
        persona_id: str,
        conversation_id: str,
        message_id: str,
    ) -> RenderedFile:
        """Produce + persist the file. Raises :class:`FileExtractionError` on failure."""
        ...


class SandboxFileRenderer:
    """Drives the SAME sandbox boundary the live ``code_execution`` tool drives.

    Reuses :class:`persona_api.sandbox.pool.SandboxPool` (the app's own composed
    pool — one substrate, shared with the live chat path) with a DEDICATED,
    isolated session per run (never the live conversation's session id), and
    persists produced bytes with the SAME F5 sidecar convention the live
    ``code_execution`` produced-file persister uses (Path-based, under
    ``workspace_root`` — see the module decisions above).
    """

    def __init__(self, *, pool: SandboxPool, workspace_root: Path) -> None:
        self._pool = pool
        self._workspace_root = workspace_root

    async def render(
        self,
        content: ExtractedContent,
        *,
        format: str,  # noqa: A002
        owner_id: str,
        persona_id: str,
        conversation_id: str,
        message_id: str,
    ) -> RenderedFile:
        if format not in RENDER_FORMATS:
            msg = f"file_extract: unsupported render format {format!r}"
            raise ValueError(msg)
        out_name = _slugify(content.title) or _OUT_NAME_FALLBACK
        expected_ref = f"{out_name}{_EXT_BY_FORMAT[format]}"
        code = render_sandbox_code(content, format, out_name)
        scope_id = f"file-extract-{message_id}"

        handle = None
        try:
            try:
                handle = await self._pool.acquire(user_id=owner_id, conversation_id=scope_id)
                limits = ResourceLimits()
                result = await self._pool.sandbox.execute(
                    code,
                    session_id=handle.session_id,
                    timeout_s=limits.wall_clock_s,
                    limits=limits,
                    network=NetworkPolicy(),
                )
            except SandboxError as exc:
                raise FileExtractionError(
                    "file_extract sandbox dispatch failed",
                    context={
                        "reason": "sandbox_execution_failed",
                        "format": format,
                        "message_id": message_id,
                        "exc_type": type(exc).__name__,
                    },
                ) from exc

            if result.outcome != "ok":
                raise FileExtractionError(
                    "file_extract sandbox render did not succeed",
                    context={
                        "reason": "sandbox_execution_failed",
                        "format": format,
                        "message_id": message_id,
                        "outcome": result.outcome,
                    },
                )

            produced = next((f for f in result.produced_files if f.path == expected_ref), None)
            if produced is None or produced.size_bytes == 0:
                raise FileExtractionError(
                    "file_extract sandbox render produced no usable file",
                    context={
                        "reason": "empty_output",
                        "format": format,
                        "message_id": message_id,
                        "expected_ref": expected_ref,
                    },
                )

            persona_workspace = self._workspace_root / owner_id / persona_id
            target = persona_workspace / "uploads" / expected_ref
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                assert handle is not None  # noqa: S101 — set by the acquire() above, never reset
                await self._pool.sandbox.copy_produced_file_to(
                    handle.session_id, produced.path, target
                )
            except SandboxError as exc:
                raise FileExtractionError(
                    "file_extract failed to copy the produced file out of the sandbox",
                    context={
                        "reason": "sandbox_execution_failed",
                        "format": format,
                        "message_id": message_id,
                        "exc_type": type(exc).__name__,
                    },
                ) from exc

            write_artifact_sidecar(
                target,
                WorkspaceArtifactMetadata(
                    source="generated",
                    type=_SIDECAR_TYPE_BY_FORMAT[format],  # type: ignore[arg-type]
                    producing_spec=_PRODUCING_SPEC_BY_FORMAT[format],  # type: ignore[arg-type]
                    conversation_id=conversation_id,
                    created_at=utcnow(),
                    original_name=expected_ref,
                ),
            )

            return RenderedFile(
                workspace_path=f"uploads/{expected_ref}",
                media_type=_MEDIA_TYPE_BY_FORMAT[format],
                size_bytes=produced.size_bytes,
                format=format,
            )
        finally:
            if handle is not None:
                await self._pool.release(handle)


# ----- the handler ------------------------------------------------------------


class FileExtractHandler:
    """Extracts substance from one message, decides format, renders via the sandbox."""

    def __init__(
        self,
        *,
        extractor: Callable[[list[ConversationMessage]], Awaitable[ExtractedContent | None]],
        renderer: FileExtractRenderer,
        repository: FileExtractRepository,
        episodic_query: Callable[[str, str], tuple[str, ...]] | None = None,
        event_channel: UserEventChannel | None = None,
    ) -> None:
        self._extract = extractor
        self._renderer = renderer
        self._repo = repository
        self._episodic_query = episodic_query
        self._event_channel = event_channel

    async def handle(self, payload: FileExtractJobPayload, context: JobContext) -> None:
        with context.connection() as conn:
            data = self._repo.read(
                conn, conversation_id=payload.conversation_id, message_id=payload.message_id
            )
        if data is None:
            return  # conversation/message deleted between enqueue and run — graceful no-op.
        if data.target_role != "assistant":
            # Defensive re-check — the route already 422s a non-assistant target at
            # enqueue time; a stale enqueue racing an edit/delete degrades to a
            # no-op rather than extracting the wrong thing.
            _logger.warning(
                "file_extract skipped — target is no longer an assistant message",
                conversation_id=payload.conversation_id,
                message_id=payload.message_id,
                role=data.target_role,
            )
            return

        episodic_snippets: tuple[str, ...] = ()
        if self._episodic_query is not None:
            try:
                episodic_snippets = self._episodic_query(data.persona_id, data.target_content)
            except Exception:  # noqa: BLE001 — v1 degrade: conversation-window-only on any recall failure
                _logger.warning(
                    "file_extract episodic recall failed; continuing conversation-window-only",
                    conversation_id=payload.conversation_id,
                    message_id=payload.message_id,
                )

        prompt = build_extraction_prompt(
            target_content=data.target_content,
            window_excerpt=conversation_window_excerpt(data.window),
            episodic_excerpt=episodic_context_excerpt(episodic_snippets),
        )
        # The model call runs OUTSIDE any DB transaction (never hold a conn
        # across an LLM await) — the title_refresh discipline, carried here.
        extracted = await self._extract(prompt)
        if extracted is None:
            raise FileExtractionError(
                "file_extract: extraction produced no usable content",
                context={
                    "reason": "llm_extraction_failed",
                    "conversation_id": payload.conversation_id,
                    "message_id": payload.message_id,
                },
            )
        context.meter(
            amount_micros=0,
            kind="model",
            detail={"surface": "file_extract", "message_id": payload.message_id},
        )

        fmt = decide_format(payload.format, extracted.kind)
        rendered = await self._renderer.render(
            extracted,
            format=fmt,
            owner_id=context.owner_id,
            persona_id=data.persona_id,
            conversation_id=payload.conversation_id,
            message_id=payload.message_id,
        )
        context.meter(
            amount_micros=0,
            kind="sandbox",
            detail={"surface": "file_extract", "format": rendered.format},
        )

        # Post-commit-shaped liveness ping (best-effort; see the module's
        # refresh-signal decision above) — mirrors title_refresh's own
        # `publish_sidebar_changed` call exactly.
        from persona_api.services.notifications_service import (  # noqa: PLC0415
            publish_sidebar_changed,
        )

        publish_sidebar_changed(
            self._event_channel,
            owner_id=context.owner_id,
            reason="conversation.file_extracted",
        )


def register_file_extract_handler(
    registry: JobRegistry,
    *,
    extractor: Callable[[list[ConversationMessage]], Awaitable[ExtractedContent | None]],
    renderer: FileExtractRenderer,
    repository: FileExtractRepository | None = None,
    episodic_query: Callable[[str, str], tuple[str, ...]] | None = None,
    event_channel: UserEventChannel | None = None,
) -> None:
    """Register the file-extract tenant (R9-025b; the title-refresh registration shape).

    Retry uses the job system's PLAIN default (``RetryPolicy()`` — no
    override): an LLM/doc-gen failure is treated as an ordinary transient
    background failure (rate limit, a flaky sandbox cold-start), the standard
    exponential-backoff/dead-letter ladder applies, exactly like any other job
    with no special-cased policy.
    """
    registry.register(
        JobTypeSpec(
            type=FILE_EXTRACT_JOB_TYPE,
            payload_model=FileExtractJobPayload,
            handler=FileExtractHandler(
                extractor=extractor,
                renderer=renderer,
                repository=repository if repository is not None else PgFileExtractRepository(),
                episodic_query=episodic_query,
                event_channel=event_channel,
            ),
            idempotency_key=file_extract_idempotency_key,
            retry=RetryPolicy(),
            lease=MEDIUM_LEASE,
        )
    )


def enqueue_file_extract(
    queue: JobQueue,
    *,
    owner_id: str,
    conversation_id: str,
    message_id: str,
    format: str,  # noqa: A002 — mirrors the route/payload's `format` field name
) -> JobRecord | None:
    """Enqueue one message+format extraction.

    A duplicate ``(message_id, format)`` re-request is A0's ``ON CONFLICT``
    no-op — returns ``None``, safe to call on every button click.
    """
    payload = FileExtractJobPayload(
        conversation_id=conversation_id, message_id=message_id, format=format
    )
    return queue.enqueue(
        type=FILE_EXTRACT_JOB_TYPE,
        owner_id=owner_id,
        payload=payload.model_dump(),
        idempotency_key=file_extract_idempotency_key(payload),
    )
