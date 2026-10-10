"""Refuse a hostile Office archive before any library decompresses it (D-FV0-08).

``.docx`` and ``.xlsx`` files are zip archives of XML parts. The parser libraries
(python-docx, openpyxl) open them with the stdlib :mod:`zipfile`, which sets no limit on
how many entries an archive may hold or how large they may decompress to, and then build
XML trees whose memory grows with the number of elements, not the number of bytes.

**Why the declared sizes alone are not a bound.** :class:`zipfile.ZipExtFile` does cut
its *output* at an entry's declared ``file_size``, but an unbounded ``read()`` (which
is what the libraries call) first asks zlib for up to ``ZipExtFile.MAX_N`` (1 GiB) of
output from the whole compressed member in one call, and only then truncates. Measured
on 2026-10-04: an 87 KB docx whose ``word/document.xml`` declares 4 KiB but inflates to
50 MiB peaked at 127 MiB inside python-docx before it failed.

**Why bytes alone are not a bound either.** python-docx keeps the whole document as an
lxml tree, about 300 bytes per element (measured 2026-10-04): a genuine 1,200-page report
is 8.5 MiB of XML and 281,000 elements (+68 MiB), while 16 MiB of empty ``<w:p/>`` is
2.8 million elements (+830 MiB).

So this guard does the decompression and an XML scan itself, once, before any library
sees the file:

1. **Directory.** It reads the central directory and refuses an archive shaped like an
   attack: too many entries, duplicate names, encrypted entries, compression other than
   stored or deflate, declared sizes over the limits (a tighter limit for XML parts a
   library loads whole), a missing ``[Content_Types].xml``, data before the first entry.
2. **Streams.** It inflates every entry in 64 KiB steps, counting real output, and
   refuses an entry that produces more than it declared, ends short of it, never reaches
   its final deflate block, or fails its CRC. Memory stays at one step.
3. **XML.** Every XML part streams through expat as it inflates. A part carrying a
   document type declaration is refused (Office Open XML parts never carry one, and a DTD
   is where entity bombs live), as is malformed XML. Elements in the parts a library
   loads whole are counted against one budget for the archive.

**Which parts are XML** is decided the way the libraries decide it: by the content type
``[Content_Types].xml`` gives the part (an ``Override`` for its name, else the
``Default`` for its extension), as well as by an ``.xml`` or ``.rels`` name. The
content types part is read first. Without that, a main document renamed
``word/document.bin`` and declared XML in the content types was loaded whole by
python-docx yet never scanned (second security review F6).

Worksheets in an xlsx are the exception to the whole-load budget: openpyxl's read-only
mode streams them row by row and frees each row, so their cost is bounded by the parser's
own cell budget (:mod:`persona.documents.parsers.xlsx`), not by their element count.

What it does not do: it never recurses into nested archives (a chart's embedded workbook
is legitimate). The parsers keep their own bounds on what they do after a file passes.

One place for this concern: the document parse path calls it in
:func:`persona.documents.parsers.parse_document`, and later file engines call the same
function, so a security review reads one module.
"""

from __future__ import annotations

import struct
import xml.parsers.expat
import zipfile
import zlib
from typing import TYPE_CHECKING, BinaryIO

from persona.documents.errors import CorruptDocumentError

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

__all__ = [
    "MAX_ARCHIVE_ENTRIES",
    "MAX_ENTRY_UNCOMPRESSED_BYTES",
    "MAX_TOTAL_UNCOMPRESSED_BYTES",
    "MAX_XML_ELEMENTS",
    "MAX_XML_PART_BYTES",
    "check_ooxml_archive",
]

#: The most entries one archive may hold. A large real workbook or document has a few
#: hundred parts (one per sheet, image, chart, relationship file); ten thousand leaves a
#: wide margin and still stops an archive built to exhaust the parser by count.
MAX_ARCHIVE_ENTRIES: int = 10_000

#: The most any single entry may hold uncompressed (128 MiB). Streamed worksheets and
#: binary parts (images, embedded objects) live under this cap.
MAX_ENTRY_UNCOMPRESSED_BYTES: int = 128 * 1024 * 1024

#: The most all entries together may hold uncompressed (256 MiB).
MAX_TOTAL_UNCOMPRESSED_BYTES: int = 256 * 1024 * 1024

#: The most an XML part that a library loads WHOLE may hold uncompressed (32 MiB).
#: Measured: a genuine 1,200-page report's ``word/document.xml`` is 8.5 MiB.
MAX_XML_PART_BYTES: int = 32 * 1024 * 1024

#: The most elements all whole-loaded XML parts of one archive may hold together.
#: Measured: the genuine 1,200-page report holds 281,000 in its main part; at about
#: 300 bytes per element in python-docx, this budget caps the tree near 300 MiB.
MAX_XML_ELEMENTS: int = 1_000_000

#: Every zip archive's first local file header starts with these four bytes. Requiring
#: them at offset zero refuses data prefixed before the archive (a file that is both an
#: image or a script and a zip, which :mod:`zipfile` would otherwise accept).
_LOCAL_HEADER_MAGIC = b"PK\x03\x04"

#: The Open Packaging Conventions part every OOXML package must carry.
_CONTENT_TYPES_PART = "[Content_Types].xml"

#: Bit 0 of a zip entry's general purpose flags marks it encrypted.
_ENCRYPTED_FLAG = 0x1

#: The fixed part of a local file header, and where its two variable lengths sit.
_LOCAL_HEADER_SIZE = 30
_LOCAL_HEADER_LENGTHS = struct.Struct("<HH")
_LOCAL_HEADER_LENGTHS_OFFSET = 26

#: How much compressed input is read, and how much output zlib may produce, per step.
_STEP_BYTES = 64 * 1024

#: Longest name carried into an error context.
_NAME_LIMIT = 120


def _safe_name(name: str) -> str:
    """A name fit for an error context: control characters removed, length capped.

    Entry names come from the archive, which the uploader wrote; they reach logs and
    the api's error body, so nothing that could forge a log line survives.
    """
    return "".join(c for c in name if ord(c) >= 32 and ord(c) != 127)[:_NAME_LIMIT]


def _refuse(reason: str, *, document_format: str, path: Path, **extra: str) -> CorruptDocumentError:
    """Build the refusal every check raises, with the standard document error context."""
    return CorruptDocumentError(
        f"{document_format} file refused: {reason.replace('_', ' ')}",
        context={
            "format": document_format,
            "reason": reason,
            "filename": _safe_name(path.name),
            **extra,
        },
    )


def _is_xml_name(name: str) -> bool:
    return name.lower().endswith((".xml", ".rels"))


def _is_xml_content_type(content_type: str) -> bool:
    """An XML content type a library parses. Images (``image/svg+xml``) are blobs to
    python-docx and openpyxl, never parsed, and an SVG may carry a harmless DOCTYPE."""
    media = content_type.split(";", 1)[0].strip().lower()
    return media.endswith(("/xml", "+xml")) and not media.startswith("image/")


class _ContentTypes:
    """The ``Default`` and ``Override`` entries of ``[Content_Types].xml``."""

    def __init__(self) -> None:
        self.by_extension: dict[str, str] = {}
        self.by_part: dict[str, str] = {}

    def collect(self, name: str, attributes: dict[str, str]) -> None:
        """An expat start-element callback: keep the two kinds of entry."""
        local = name.rsplit(":", 1)[-1].rsplit("}", 1)[-1]
        content_type = attributes.get("ContentType", "")
        if local == "Default" and "Extension" in attributes:
            self.by_extension[attributes["Extension"].lower()] = content_type
        elif local == "Override" and "PartName" in attributes:
            self.by_part[attributes["PartName"].lower()] = content_type

    def of(self, entry_name: str) -> str:
        """The content type of the part stored as ``entry_name`` ("" if none is given)."""
        part = "/" + entry_name.lower()
        if part in self.by_part:
            return self.by_part[part]
        extension = entry_name.rsplit("/", 1)[-1].rpartition(".")[2].lower()
        return self.by_extension.get(extension, "")

    def is_xml(self, entry_name: str) -> bool:
        """Whether a library would read this part as XML."""
        return _is_xml_name(entry_name) or _is_xml_content_type(self.of(entry_name))


def _is_streamed_part(name: str, document_format: str) -> bool:
    """Whether the library reads this part row by row instead of loading it whole."""
    return document_format == "xlsx" and name.startswith("xl/worksheets/")


class _XmlRefusalError(Exception):
    """Raised from inside an expat handler to stop the scan with a reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _ElementBudget:
    """The element count shared by every whole-loaded XML part of one archive."""

    def __init__(self) -> None:
        self.used = 0


def check_ooxml_archive(path: Path, *, document_format: str) -> None:
    """Refuse an Office archive that is unsafe to hand to a parser library.

    Args:
        path: The archive on disk.
        document_format: The format being parsed (``"docx"`` or ``"xlsx"``), carried in
            the error context and used to tell streamed parts from whole-loaded ones.

    Raises:
        CorruptDocumentError: With ``context["reason"]`` one of ``not_a_zip``,
            ``too_many_entries``, ``duplicate_entry``, ``encrypted_entry``,
            ``unsupported_compression``, ``entry_too_large``, ``xml_part_too_large``,
            ``archive_too_large``, ``missing_part``, ``size_mismatch``,
            ``xml_doctype``, ``malformed_xml`` or ``too_many_elements``.
    """
    with path.open("rb") as handle:
        if handle.read(len(_LOCAL_HEADER_MAGIC)) != _LOCAL_HEADER_MAGIC:
            raise _refuse("not_a_zip", document_format=document_format, path=path)
        try:
            with zipfile.ZipFile(handle) as archive:
                entries = archive.infolist()
        except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, ValueError) as exc:
            raise _refuse("not_a_zip", document_format=document_format, path=path) from exc
        _check_directory(entries, document_format=document_format, path=path)
        budget = _ElementBudget()
        content_types = _ContentTypes()
        types_entry = next(entry for entry in entries if entry.filename == _CONTENT_TYPES_PART)
        _check_entry_stream(
            handle,
            types_entry,
            document_format=document_format,
            path=path,
            budget=budget,
            is_xml=True,
            collect=content_types.collect,
        )
        for entry in entries:
            if entry is types_entry:
                continue
            is_xml = content_types.is_xml(entry.filename)
            if (
                is_xml
                and not _is_streamed_part(entry.filename, document_format)
                and entry.file_size > MAX_XML_PART_BYTES
            ):
                # A part named .xml was checked in the directory; this catches one that is
                # XML only by its declared content type.
                raise _refuse(
                    "xml_part_too_large",
                    document_format=document_format,
                    path=path,
                    entry=_safe_name(entry.filename),
                    limit=str(MAX_XML_PART_BYTES),
                )
            _check_entry_stream(
                handle,
                entry,
                document_format=document_format,
                path=path,
                budget=budget,
                is_xml=is_xml,
            )


def _check_directory(entries: list[zipfile.ZipInfo], *, document_format: str, path: Path) -> None:
    """Refuse on what the central directory declares, before anything is decompressed."""
    if len(entries) > MAX_ARCHIVE_ENTRIES:
        raise _refuse(
            "too_many_entries",
            document_format=document_format,
            path=path,
            limit=str(MAX_ARCHIVE_ENTRIES),
        )
    names = [entry.filename for entry in entries]
    if len(set(names)) != len(names):
        raise _refuse("duplicate_entry", document_format=document_format, path=path)
    total = 0
    for entry in entries:
        if entry.flag_bits & _ENCRYPTED_FLAG:
            raise _refuse("encrypted_entry", document_format=document_format, path=path)
        if entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise _refuse("unsupported_compression", document_format=document_format, path=path)
        if entry.file_size > MAX_ENTRY_UNCOMPRESSED_BYTES:
            raise _refuse(
                "entry_too_large",
                document_format=document_format,
                path=path,
                limit=str(MAX_ENTRY_UNCOMPRESSED_BYTES),
            )
        if (
            _is_xml_name(entry.filename)
            and not _is_streamed_part(entry.filename, document_format)
            and entry.file_size > MAX_XML_PART_BYTES
        ):
            raise _refuse(
                "xml_part_too_large",
                document_format=document_format,
                path=path,
                entry=_safe_name(entry.filename),
                limit=str(MAX_XML_PART_BYTES),
            )
        total += entry.file_size
    if total > MAX_TOTAL_UNCOMPRESSED_BYTES:
        raise _refuse(
            "archive_too_large",
            document_format=document_format,
            path=path,
            limit=str(MAX_TOTAL_UNCOMPRESSED_BYTES),
        )
    if _CONTENT_TYPES_PART not in names:
        raise _refuse("missing_part", document_format=document_format, path=path)


def _xml_scanner(
    *,
    counted: bool,
    budget: _ElementBudget,
    collect: Callable[[str, dict[str, str]], None] | None = None,
) -> xml.parsers.expat.XMLParserType:
    """An expat parser that refuses a DTD and, for whole-loaded parts, counts elements.

    ``collect`` also sees every element (the content types part is read through it).
    """
    parser = xml.parsers.expat.ParserCreate()

    def doctype(*_args: object) -> None:
        raise _XmlRefusalError("xml_doctype")

    parser.StartDoctypeDeclHandler = doctype
    if counted or collect is not None:

        def element(name: str, attributes: dict[str, str]) -> None:
            if counted:
                budget.used += 1
                if budget.used > MAX_XML_ELEMENTS:
                    raise _XmlRefusalError("too_many_elements")
            if collect is not None:
                collect(name, attributes)

        parser.StartElementHandler = element
    return parser


def _check_entry_stream(
    handle: BinaryIO,
    entry: zipfile.ZipInfo,
    *,
    document_format: str,
    path: Path,
    budget: _ElementBudget,
    is_xml: bool,
    collect: Callable[[str, dict[str, str]], None] | None = None,
) -> None:
    """Decompress one entry in bounded steps; refuse unless it yields exactly what it declared.

    The entry's bytes are located through its local header, read ``_STEP_BYTES`` at a
    time, and inflated with an output cap of ``_STEP_BYTES`` per call, so memory never
    exceeds one step whatever the stream claims, and inflating stops one step past the
    declared size at the latest. Output zlib still holds when the input runs out is
    drained the same way, a step at a time. Output past the declared size, output short
    of it, a deflate stream that never reaches its final block, compressed bytes the file
    does not hold, and a CRC that does not match are all refused. An XML part's output is
    scanned by expat as it is produced.
    """
    entry_name = _safe_name(entry.filename)

    def mismatch() -> CorruptDocumentError:
        return _refuse(
            "size_mismatch", document_format=document_format, path=path, entry=entry_name
        )

    handle.seek(entry.header_offset)
    header = handle.read(_LOCAL_HEADER_SIZE)
    if len(header) != _LOCAL_HEADER_SIZE:
        raise mismatch()
    name_length, extra_length = _LOCAL_HEADER_LENGTHS.unpack_from(
        header, _LOCAL_HEADER_LENGTHS_OFFSET
    )
    handle.seek(entry.header_offset + _LOCAL_HEADER_SIZE + name_length + extra_length)

    scanner = (
        _xml_scanner(
            counted=not _is_streamed_part(entry.filename, document_format),
            budget=budget,
            collect=collect,
        )
        if is_xml
        else None
    )
    inflater = (
        zlib.decompressobj(-zlib.MAX_WBITS) if entry.compress_type == zipfile.ZIP_DEFLATED else None
    )
    remaining_input = entry.compress_size
    produced = 0
    crc = 0
    pending = b""
    try:
        while True:
            if pending:
                data = pending
            elif remaining_input > 0:
                data = handle.read(min(_STEP_BYTES, remaining_input))
                if not data:
                    # The directory claims more compressed bytes than the file holds.
                    raise mismatch()
                remaining_input -= len(data)
            elif inflater is not None and not inflater.eof:
                # All input is consumed, but zlib can still hold output it could not hand
                # back inside the last 64 KiB step. Drain it, one step at a time, still
                # counted against the declared size.
                data = b""
            else:
                break
            if inflater is None:
                output, pending = data, b""
            else:
                output = inflater.decompress(data, _STEP_BYTES)
                pending = inflater.unconsumed_tail
                if not data and not output:
                    break  # Drained; nothing more will come without more input.
            produced += len(output)
            if produced > entry.file_size:
                raise mismatch()
            crc = zlib.crc32(output, crc)
            if scanner is not None and output:
                scanner.Parse(output, False)
        if inflater is not None and not inflater.eof:
            # The deflate stream never reached its final block.
            raise mismatch()
        if produced != entry.file_size or crc != entry.CRC:
            raise mismatch()
        if scanner is not None:
            scanner.Parse(b"", True)
    except _XmlRefusalError as refusal:
        raise _refuse(
            refusal.reason, document_format=document_format, path=path, entry=entry_name
        ) from None
    except xml.parsers.expat.ExpatError as exc:
        raise _refuse(
            "malformed_xml", document_format=document_format, path=path, entry=entry_name
        ) from exc
