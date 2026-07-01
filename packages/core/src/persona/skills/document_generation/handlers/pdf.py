"""``pdf`` format handler — was ``builtin/pdf_generation/`` (D-24-1)."""

from __future__ import annotations

from persona.skills.document_generation.protocol import FormatHandler

#: PDF report. The SKILL.md teaches a **try-import**: prefer ``reportlab`` (rich
#: text/tables via platypus flowables — present on the custom doc-gen template,
#: Spec P5) and fall back to the always-present ``matplotlib`` PdfPages backend on
#: ``ModuleNotFoundError``. Egress stays disabled (no runtime ``pip install`` — the
#: libs are baked into the template at BUILD time, P5); the branch is at import
#: time, so the same static instructions produce a real ``.pdf`` on either template.
PDF = FormatHandler(
    format_key="pdf",
    output_extension=".pdf",
    library="reportlab||matplotlib",
    supplement_topics=("flowables", "pagination", "images"),
)

__all__ = ["PDF"]
