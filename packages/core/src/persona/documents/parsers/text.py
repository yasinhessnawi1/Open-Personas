"""Plain-text / Markdown / source-code parser (spec 14 T06).

Stdlib-only — no parser library, no extra dependency. Reads the file as
UTF-8 with replacement on decode errors (fail-safe per the §6 convention:
partial + flag, never crash). Splits into :class:`DocumentSection` units
along format-appropriate boundaries:

- **Plain text** (``.txt``): paragraphs (blank-line-separated).
- **Markdown** (``.md``): top-level heading-bounded sections, with the
  heading text stamped onto each section's ``section`` field so retrieved
  chunks can cite their source heading.
- **Source code** (``.py`` / ``.js`` / ``.ts`` / etc.): the whole file
  wrapped in a language-aware fence so the model reads it as code, not as
  prose. The chunker's token-cap fallback handles long files.

Empty-extraction handling:

- A file that's *literally empty* on disk → ``CorruptDocumentError`` with
  ``reason="empty_file"`` (different from "extraction yielded nothing"
  — the file is genuinely empty).
- A file with non-empty bytes but no text after decode (e.g. an all-NUL
  binary that's mislabelled ``.txt``) → ``CorruptDocumentError`` with
  ``reason="empty_after_decode"``. This is criterion #8's "empty
  extraction from a non-empty file is flagged".
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.documents.chunker import DocumentSection
from persona.documents.errors import CorruptDocumentError
from persona.documents.parsers import FLAG_CORRUPT, ParseResult

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "CODE_FENCE_LANGUAGE_BY_EXTENSION",
    "parse_text",
]

#: File-extension → Markdown code-fence language tag. Used when the file is
#: detected as source code so the model reads it under the right syntax.
CODE_FENCE_LANGUAGE_BY_EXTENSION: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "jsx",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".kt": "kotlin",
    ".swift": "swift",
    ".rb": "ruby",
    ".php": "php",
    ".c": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".h": "c",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".scala": "scala",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "bash",
    ".sql": "sql",
    ".html": "html",
    ".css": "css",
    ".scss": "scss",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".json": "json",
    ".xml": "xml",
}


def parse_text(path: Path) -> ParseResult:
    """Parse a ``.txt`` / ``.md`` / source-code file.

    Args:
        path: Path to the file. Must exist; the caller (the upload service
            and the dispatcher in T11) is responsible for validating
            existence + size limits before calling.

    Returns:
        :class:`ParseResult` carrying one :class:`DocumentSection` per
        paragraph (.txt) / per Markdown heading-block (.md) / one whole-
        file section wrapped in a language fence (source code).

    Raises:
        CorruptDocumentError: The file is genuinely empty, or all bytes
            decode to whitespace / control characters only (criterion #8's
            "empty extraction from non-empty file is flagged" path).
    """
    size_bytes = path.stat().st_size
    extension = path.suffix.lower()
    filename = path.name

    raw_bytes = path.read_bytes()
    if not raw_bytes:
        raise CorruptDocumentError(
            "file is empty",
            context={
                "format": _format_for_extension(extension),
                "reason": "empty_file",
                "filename": filename,
            },
        )

    # UTF-8 with replacement — never crash on decode errors. Track whether
    # any replacement happened so the flag is set.
    text_strict_failed = False
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        text_strict_failed = True
        text = raw_bytes.decode("utf-8", errors="replace")

    # Spec WIN T2: a file written on Windows (CRLF) or classic Mac (lone CR) splits into
    # the same paragraphs, headings and lines as one written with LF. Without this a
    # CRLF .txt uploaded to any server parsed as a single paragraph.
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    if not text.strip():
        # Non-empty bytes that decode to nothing useful — criterion #8.
        raise CorruptDocumentError(
            "extraction yielded no text",
            context={
                "format": _format_for_extension(extension),
                "reason": "empty_after_decode",
                "filename": filename,
            },
        )

    flags: list[str] = []
    if text_strict_failed:
        flags.append(FLAG_CORRUPT)

    sections = _build_sections(text, extension=extension)

    return ParseResult(
        sections=tuple(sections),
        flags=tuple(flags),
        size_bytes=size_bytes,
    )


def _format_for_extension(extension: str) -> str:
    """Map a file extension to the format string used in error contexts."""
    if extension == ".md":
        return "md"
    if extension == ".txt":
        return "txt"
    return "code"


def _build_sections(text: str, *, extension: str) -> list[DocumentSection]:
    """Dispatch to format-specific section-building."""
    if extension == ".md":
        return _build_markdown_sections(text)
    if extension == ".txt" or extension == "":
        return _build_plain_text_sections(text)
    # Otherwise treat as source code.
    return _build_code_sections(text, extension=extension)


def _build_plain_text_sections(text: str) -> list[DocumentSection]:
    """Split plain text into paragraph-bounded sections.

    A paragraph is a run of non-empty lines separated from its neighbours
    by one or more blank lines. Empty paragraphs are dropped.
    """
    paragraphs = [p.strip() for p in text.split("\n\n")]
    return [DocumentSection(text=p) for p in paragraphs if p]


def _build_markdown_sections(text: str) -> list[DocumentSection]:
    """Split Markdown into heading-bounded sections.

    Each top-level heading (``#`` / ``##`` / ``###`` etc.) starts a new
    section whose ``section`` field is the heading text (without the ``#``
    prefix). Content before the first heading becomes its own section with
    no heading stamp.
    """
    sections: list[DocumentSection] = []
    current_heading: str | None = None
    current_lines: list[str] = []

    def _flush() -> None:
        nonlocal current_lines
        body = "\n".join(current_lines).strip()
        if body or current_heading:
            full_body = body
            if current_heading:
                full_body = f"{current_heading}\n\n{body}" if body else current_heading
            if full_body.strip():
                sections.append(DocumentSection(text=full_body, section=current_heading))
        current_lines = []

    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#") and " " in stripped:
            # Heading line — flush the previous section.
            _flush()
            # Extract heading text without the leading ``#`` markers.
            current_heading = stripped.lstrip("#").strip()
            continue
        current_lines.append(line)

    _flush()
    return sections


def _build_code_sections(text: str, *, extension: str) -> list[DocumentSection]:
    """Wrap source code in a language-aware Markdown fence.

    One section per file (the chunker handles long files via token-cap
    fallback). The fence ensures the model reads the content as code
    under the right syntax — important for the persona's reasoning about
    code.
    """
    language = CODE_FENCE_LANGUAGE_BY_EXTENSION.get(extension, "")
    fence_header = f"```{language}" if language else "```"
    fenced = f"{fence_header}\n{text}\n```"
    return [DocumentSection(text=fenced)]
