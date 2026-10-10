"""Test helper: documents for driving the real parse worker pool (D-FV0-31).

Hostile files that pass every bound in the parsers yet still cost too much, so only a
worker limit stops them, plus genuine files that must still parse.
"""

from __future__ import annotations

import io
import zipfile
import zlib
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[2] / "core" / "tests" / "fixtures" / "documents"


def _pdf(objects: list[bytes]) -> bytes:
    """Serialise numbered objects (object 1 is the catalog) with a correct xref."""
    out = bytearray(b"%PDF-1.7\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


def flate_bomb_pdf(path: Path, *, pages: int = 2_000, inflated_mib: int = 64) -> Path:
    """A small PDF whose pages all share one content stream that inflates to ``inflated_mib``.

    Every bound holds: the page count is under the limits, and the stream is under
    pypdf's per-stream output cap. Reading the text still means inflating and
    interpreting the stream once per page, which is minutes of CPU. Only the worker's
    CPU limit or the wall-clock kill stops it.
    """
    compressor = zlib.compressobj(9)
    chunk = b"0 0 m " * (1024 * 1024 // 6)
    stream = compressor.compress(b"BT /F1 12 Tf 72 720 Td (x) Tj ET\n")
    stream += b"".join(compressor.compress(chunk) for _ in range(inflated_mib))
    stream += compressor.flush()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(stream) + stream + b"\nendstream",
    ]
    kids = []
    for _ in range(pages):
        kids.append(len(objects) + 1)
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 3 0 R >> >> /Contents 4 0 R >>"
        )
    objects[1] = b"<< /Type /Pages /Count %d /Kids [%s] >>" % (
        pages,
        b" ".join(b"%d 0 R" % kid for kid in kids),
    )
    path.write_bytes(_pdf(objects))
    return path


def text_bomb_pdf(path: Path, *, pages: int = 60, text_bytes: int = 1_000_000) -> Path:
    """The second review's ``textbomb_60p_1MB.pdf``: pages sharing one stream of text.

    12 KB of file; before the text budget it parsed to 60 million characters, all of
    which crossed to the api.
    """
    lorem = b"lorem ipsum dolor sit amet "
    text = lorem * (text_bytes // len(lorem))
    stream = zlib.compress(b"BT /F1 12 Tf 10 10 Td (" + text + b") Tj ET", 9)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(stream) + stream + b"\nendstream",
    ]
    kids = []
    for _ in range(pages):
        kids.append(len(objects) + 1)
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 3 0 R >> >> /Contents 4 0 R >>"
        )
    objects[1] = b"<< /Type /Pages /Count %d /Kids [%s] >>" % (
        pages,
        b" ".join(b"%d 0 R" % kid for kid in kids),
    )
    path.write_bytes(_pdf(objects))
    return path


_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" '
    'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/'
    'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    "</Types>"
)
_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
    'relationships/officeDocument" Target="word/document.xml"/>'
    "</Relationships>"
)


def paragraph_flood_docx(path: Path, *, elements: int = 999_000) -> Path:
    """A docx just inside the archive guard's element budget: a large DOM, legally.

    python-docx holds every element in memory, so this needs a few hundred megabytes
    (measured: 236 MB resident). A worker with a lower memory limit cannot parse it.
    """
    body = "<w:p><w:r><w:t>x</w:t></w:r></w:p>" * (elements // 3 - 2)
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>" + body + "</w:body></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("_rels/.rels", _RELS)
        archive.writestr("word/document.xml", document)
    path.write_bytes(buffer.getvalue())
    return path


def genuine(path: Path, fixture: str) -> Path:
    """Copy a genuine fixture document (``sample.docx``, ``scanned-like.pdf``, ...)."""
    path.write_bytes((FIXTURES / fixture).read_bytes())
    return path
