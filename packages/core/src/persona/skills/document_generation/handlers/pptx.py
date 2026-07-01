"""``pptx`` format handler — was ``builtin/pptx_generation/`` (D-24-1)."""

from __future__ import annotations

from persona.skills.document_generation.protocol import FormatHandler

#: PowerPoint ``.pptx``. ``python-pptx`` ships on the custom doc-gen template but
#: NOT the default one (and can't be installed offline — egress disabled). The
#: SKILL.md's pptx section is **fidelity-fenced** (Spec P5, P5-D-4): when the
#: custom template is active it teaches producing real ``.pptx`` (with a try-import
#: guard for the mismatch edge); otherwise it offers a ``docx``/``md`` slide
#: alternative up-front. ``library`` records the conditional status.
PPTX = FormatHandler(
    format_key="pptx",
    output_extension=".pptx",
    library="python-pptx (custom template) || unavailable-offline",
    supplement_topics=("layouts", "charts", "theme"),
)

__all__ = ["PPTX"]
