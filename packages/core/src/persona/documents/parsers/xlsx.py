"""XLSX parser (spec 14 T09).

Uses ``openpyxl==3.1.5`` (Spec 12 sandbox alignment per
:file:`packages/core/src/persona/sandbox/image/requirements.txt`). Gated
behind the ``[documents]`` extra (D-14-X-documents-extra); the import is
lazy inside :func:`parse_xlsx` so the package imports cleanly without the
extra. Missing-library failure surfaces as
:class:`~persona.documents.errors.MissingDependencyError`.

Extraction strategy:

- One :class:`DocumentSection` per **sheet**. The section's
  :attr:`DocumentSection.sheet` field carries the sheet name.
- Per-sheet text is a pipe-separated table (header row + data rows), with
  D-14-3's row cap applied per-sheet (default 1000; env override
  ``PERSONA_DOC_SPREADSHEET_ROW_CAP`` shared with the CSV parser).
- Oversize sheets emit a summary + first/last 50 rows + the
  :data:`FLAG_ROW_CAP_TRUNCATED` flag (the §8 tabular-boundary signal).
- :attr:`ParseResult.sheet_names` is populated with every sheet's name for
  the T16 synopsis.

Fail-safe handling:

- ``openpyxl.load_workbook`` raises ``InvalidFileException`` /
  ``BadZipFile`` / similar on corrupt files. Caught at the adapter boundary
  and re-raised as :class:`~persona.documents.errors.CorruptDocumentError`.
- A workbook with no sheets, or sheets that yield no text, raises
  ``CorruptDocumentError`` with ``reason="empty_after_decode"``.
"""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING, Any

from persona.documents.chunker import DocumentSection
from persona.documents.errors import (
    CorruptDocumentError,
    MissingDependencyError,
)
from persona.documents.parsers import (
    FLAG_PARTIAL_EXTRACTION,
    FLAG_ROW_CAP_TRUNCATED,
    MAX_DOCUMENT_TEXT_CHARS,
    ParseResult,
)
from persona.documents.parsers.csv import SAMPLE_ROWS_PER_END, resolve_row_cap

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "MAX_CELL_CHARS",
    "MAX_SHEET_COLUMNS",
    "MAX_SHEET_ROWS",
    "MAX_WORKBOOK_CELLS",
    "MAX_WORKBOOK_TEXT_CHARS",
    "parse_xlsx",
]


def _import_openpyxl() -> Any:  # noqa: ANN401
    """Lazy-import ``openpyxl`` with a clear MissingDependencyError on failure."""
    try:
        import openpyxl  # noqa: PLC0415 — deliberate lazy import
    except ImportError as exc:
        raise MissingDependencyError(
            "openpyxl is not installed",
            context={
                "format": "xlsx",
                "install_hint": "pip install persona-core[documents]",
            },
        ) from exc
    return openpyxl


#: The most columns read from any row. Columns past it are dropped and the sheet is
#: flagged partial. openpyxl pads every row to the width a sheet DECLARES, so without an
#: explicit width a 1.5 KB file declaring ``A1:XFD1000`` pads 16,384 columns into each
#: row (measured 2026-10-04: 383 MiB and 49 MB of text).
MAX_SHEET_COLUMNS: int = 256

#: The most rows read from any one sheet. Rows past it are not read; the summary says so.
#: A genuine 150,000-row ledger fits.
MAX_SHEET_ROWS: int = 250_000

#: The most cells read across the whole workbook, padding included: the work budget. A
#: genuine 150,000 x 12 ledger is 1.8 million cells.
MAX_WORKBOOK_CELLS: int = 3_000_000

#: The longest a single cell's text may be in the extracted table. One shared string can
#: be referenced by every cell in a sheet, so cell text is amplified by reference.
MAX_CELL_CHARS: int = 256

#: The most characters of table text one workbook may produce: the document text budget
#: every format shares. Rows past it are not kept.
MAX_WORKBOOK_TEXT_CHARS: int = MAX_DOCUMENT_TEXT_CHARS


class _Budget:
    """Work and text spent so far across one workbook."""

    def __init__(self) -> None:
        self.cells = 0
        self.chars = 0


def parse_xlsx(path: Path) -> ParseResult:
    """Parse an ``.xlsx`` workbook to per-sheet text-table sections.

    Every sheet is read with an explicit width and height, row by row, keeping only the
    rows the table shows (the head, plus a ring of the last rows); nothing scales with
    what the sheet merely declares.

    Args:
        path: Path to the ``.xlsx`` file.

    Returns:
        :class:`ParseResult` with one :class:`DocumentSection` per sheet.
        :attr:`ParseResult.sheet_names` lists every sheet's name (used by
        the T16 synopsis). :attr:`ParseResult.flags` includes
        :data:`FLAG_ROW_CAP_TRUNCATED` when at least one sheet exceeded the
        row cap, and :data:`FLAG_PARTIAL_EXTRACTION` when a read bound stopped
        reading before the end of a sheet.

    Raises:
        MissingDependencyError: ``openpyxl`` is not installed.
        CorruptDocumentError: The file isn't a valid xlsx, a sheet cannot be read,
            or extraction yielded no text (criterion #8). ``MemoryError``,
            ``RecursionError`` and other non-``Exception`` signals are never
            relabelled; they propagate to the caller.
    """
    openpyxl = _import_openpyxl()
    size_bytes = path.stat().st_size
    filename = path.name

    if size_bytes == 0:
        raise CorruptDocumentError(
            "file is empty",
            context={"format": "xlsx", "reason": "empty_file", "filename": filename},
        )

    try:
        workbook = openpyxl.load_workbook(filename=str(path), data_only=True, read_only=True)
    except _NOT_A_FILE_PROBLEM:
        raise
    except Exception as exc:  # noqa: BLE001 — adapter-boundary catch
        reason = _classify_open_failure(exc)
        raise CorruptDocumentError(
            "could not open xlsx",
            context={"format": "xlsx", "reason": reason, "filename": filename},
        ) from exc

    try:
        return _extract(workbook, filename=filename, size_bytes=size_bytes)
    finally:
        workbook.close()


#: Failures that say something about the PROCESS, not the file. They are never turned
#: into "this file is corrupt": a MemoryError must reach the caller as itself.
_NOT_A_FILE_PROBLEM = (MemoryError, RecursionError)


def _extract(workbook: Any, *, filename: str, size_bytes: int) -> ParseResult:  # noqa: ANN401
    sheet_names = tuple(workbook.sheetnames)
    if not sheet_names:
        raise CorruptDocumentError(
            "workbook has no sheets",
            context={
                "format": "xlsx",
                "reason": "no_sheets",
                "filename": filename,
            },
        )

    row_cap = resolve_row_cap()
    budget = _Budget()
    sections: list[DocumentSection] = []
    flags: list[str] = []

    for sheet_name in sheet_names:
        sheet = workbook[sheet_name]
        try:
            table = _read_sheet(sheet, row_cap=row_cap, budget=budget)
        except _NOT_A_FILE_PROBLEM:
            raise
        except Exception as exc:  # noqa: BLE001 (adapter-boundary catch)
            # Read-only mode parses each sheet lazily, so a malformed or hostile sheet
            # (an XML entity bomb that expat stops at its amplification limit, for one)
            # fails HERE rather than at open. Without this catch it escaped as a raw
            # parser error and reached the api as a 500.
            raise CorruptDocumentError(
                "could not read a sheet",
                context={"format": "xlsx", "reason": "unreadable_sheet", "filename": filename},
            ) from exc
        if table.partial and FLAG_PARTIAL_EXTRACTION not in flags:
            flags.append(FLAG_PARTIAL_EXTRACTION)
        section_text, truncated = _format_sheet_table(table, row_cap=row_cap)
        if not section_text.strip():
            continue
        if truncated and FLAG_ROW_CAP_TRUNCATED not in flags:
            flags.append(FLAG_ROW_CAP_TRUNCATED)
        sections.append(DocumentSection(text=section_text, sheet=sheet_name))

    if not sections:
        raise CorruptDocumentError(
            "extraction yielded no text",
            context={
                "format": "xlsx",
                "reason": "empty_after_decode",
                "filename": filename,
            },
        )

    return ParseResult(
        sections=tuple(sections),
        flags=tuple(flags),
        sheet_names=sheet_names,
        size_bytes=size_bytes,
    )


def _classify_open_failure(exc: BaseException) -> str:
    """Map an openpyxl open failure to a stable ``reason`` discriminator."""
    name = type(exc).__name__
    if "InvalidFile" in name:
        return "not_an_xlsx"
    if "Zip" in name or "BadZip" in name:
        return "not_a_zip"
    return "unknown"


class _SheetTable:
    """What the table shows of one sheet: header, kept head rows, the last rows.

    ``data_rows`` counts every data row up to the last non-empty one that was read;
    ``head`` keeps the first rows (all of them while the sheet fits the row cap and the
    text budget); ``tail`` keeps the last :data:`SAMPLE_ROWS_PER_END`.
    """

    def __init__(self) -> None:
        self.header: list[str] | None = None
        self.head: list[list[str]] = []
        self.head_complete = True
        self.tail: deque[list[str]] = deque(maxlen=SAMPLE_ROWS_PER_END)
        self.data_rows = 0
        self.partial = False
        self.stopped_after_rows: int | None = None
        self.columns_dropped = False


def _declared_width(sheet: Any) -> int | None:  # noqa: ANN401
    """The sheet's declared column count, never more than :data:`MAX_SHEET_COLUMNS`.

    Read from the ``<dimension>`` openpyxl already parsed; never computed by scanning.
    ``None`` when the sheet declares nothing (openpyxl's write-only mode writes no usable
    dimension): openpyxl then sizes each row to its own last cell, the rows are cut to
    the cap here, and every cell it built, padding included, is charged to the budget.
    """
    declared = getattr(sheet, "max_column", None)
    if not isinstance(declared, int) or declared < 1:
        return None
    return min(declared, MAX_SHEET_COLUMNS)


def _cell_text(value: object) -> str:
    if value is None:
        return ""
    text = str(value)
    if len(text) > MAX_CELL_CHARS:
        return text[: MAX_CELL_CHARS - 1] + "\u2026"
    return text


def _read_sheet(sheet: Any, *, row_cap: int, budget: _Budget) -> _SheetTable:  # noqa: ANN401
    """Stream one sheet's rows within the read bounds; keep only what the table shows."""
    table = _SheetTable()
    width = _declared_width(sheet)
    declared = getattr(sheet, "max_column", None)
    if isinstance(declared, int) and declared > MAX_SHEET_COLUMNS:
        table.columns_dropped = True
        table.partial = True

    pending_empty = 0
    rows_read = 0
    # One row past the limit is requested only to learn whether the sheet goes on.
    for raw in sheet.iter_rows(
        min_row=1, max_row=MAX_SHEET_ROWS + 1, min_col=1, max_col=width, values_only=True
    ):
        rows_read += 1
        if rows_read > MAX_SHEET_ROWS:
            table.partial = True
            table.stopped_after_rows = MAX_SHEET_ROWS
            break
        budget.cells += len(raw)
        if budget.cells > MAX_WORKBOOK_CELLS:
            table.partial = True
            table.stopped_after_rows = rows_read - 1
            break
        if len(raw) > MAX_SHEET_COLUMNS:
            raw = raw[:MAX_SHEET_COLUMNS]
            table.columns_dropped = True
            table.partial = True
        cells = [_cell_text(value) for value in raw]
        if not any(c.strip() for c in cells):
            if table.header is not None:
                pending_empty += 1
            continue
        if table.header is None:
            table.header = cells
            budget.chars += _text_cost(cells)
            continue
        # Rows that were empty BETWEEN non-empty rows are real rows of the table.
        for _ in range(pending_empty):
            _keep(table, [], row_cap=row_cap, budget=budget)
        pending_empty = 0
        _keep(table, cells, row_cap=row_cap, budget=budget)
    return table


def _used_width(cells: list[str]) -> int:
    """Columns up to the last non-empty cell: what the row really fills."""
    for index in range(len(cells) - 1, -1, -1):
        if cells[index].strip():
            return index + 1
    return 0


def _text_cost(cells: list[str]) -> int:
    """Characters the row renders to: its cells plus the `` | `` between them."""
    used = _used_width(cells)
    return sum(len(c) for c in cells[:used]) + 3 * used


def _keep(table: _SheetTable, cells: list[str], *, row_cap: int, budget: _Budget) -> None:
    table.data_rows += 1
    table.tail.append(cells)
    if not table.head_complete:
        return
    cost = _text_cost(cells)
    if len(table.head) + 1 >= row_cap or budget.chars + cost > MAX_WORKBOOK_TEXT_CHARS:
        # The full table no longer fits: from here only the first and last rows show.
        table.head_complete = False
        if budget.chars + cost > MAX_WORKBOOK_TEXT_CHARS:
            table.partial = True
        del table.head[SAMPLE_ROWS_PER_END:]
        return
    table.head.append(cells)
    budget.chars += cost


def _format_sheet_table(table: _SheetTable, *, row_cap: int) -> tuple[str, bool]:
    """Render a sheet as a pipe-separated text table; returns ``(text, truncated)``.

    Mirrors the CSV parser's table so read-path output is consistent across the two
    tabular formats. ``row_cap`` is kept in the signature for that symmetry; the head
    was already cut to it while reading.
    """
    del row_cap
    if table.header is None:
        return ("", False)
    # The width the rows really fill, not the width the sheet declared: a sheet that
    # declares 256 columns and fills two renders two.
    width = max(_used_width(row) for row in (table.header, *table.head, *table.tail))
    header = table.header[:width]

    if table.head_complete and not table.partial:
        formatted = " | ".join(header)
        for row in table.head:
            formatted += "\n" + " | ".join(_normalise_row(row, width=width))
        return (formatted, False)

    head = table.head[:SAMPLE_ROWS_PER_END]
    tail = list(table.tail)
    # Never repeat a row the head already shows.
    unseen = min(len(tail), max(0, table.data_rows - len(head)))
    shown_tail = tail[len(tail) - unseen :]
    hidden_count = table.data_rows - len(head) - len(shown_tail)

    count = f"{table.data_rows} data rows"
    if table.stopped_after_rows is not None:
        stopped = table.stopped_after_rows
        count = f"{table.data_rows} data rows read; reading stopped after row {stopped}"
    if table.columns_dropped:
        count += f"; only the first {MAX_SHEET_COLUMNS} columns are shown"
    lines = [f"[Sheet summary: {width} columns, {count}]", " | ".join(header)]
    for row in head:
        lines.append(" | ".join(_normalise_row(row, width=width)))
    if hidden_count > 0:
        lines.append(f"... {hidden_count} rows hidden ...")
    for row in shown_tail:
        lines.append(" | ".join(_normalise_row(row, width=width)))
    lines.append(
        "(Truncated for prompt budget. Use the code-execution sandbox for "
        "computation over the full data; spec section 8 tabular boundary.)"
    )
    return ("\n".join(lines), True)


def _normalise_row(row: list[str], *, width: int) -> list[str]:
    """Pad or truncate a row to ``width`` cells so the table is rectangular."""
    if len(row) < width:
        return [*row, *([""] * (width - len(row)))]
    if len(row) > width:
        return row[:width]
    return row
