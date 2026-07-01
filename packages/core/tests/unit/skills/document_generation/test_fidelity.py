"""Spec P5 A3 — the document_generation fidelity transform (P5-D-4).

Proves BOTH variants non-vacuously against the REAL scanned SKILL.md: the
full-fidelity path (custom template → produce real ``.pptx`` via python-pptx) and
the offline-degrade path (default template → offer ``.docx``/``.md`` up-front) —
each actually exercised, so neither branch is asserted-and-assumed. Also guards
the marker stripping, the non-doc-gen passthrough, and the token recompute.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.skills import BUILTIN_ROOT, SkillScanner
from persona.skills.document_generation import (
    apply_docgen_fidelity,
    resolve_fidelity_markers,
)

if TYPE_CHECKING:
    from persona.schema.skills import SkillSpec


def _docgen_spec() -> SkillSpec:
    specs = SkillScanner([BUILTIN_ROOT]).scan(["document_generation"])
    assert len(specs) == 1
    return specs[0]


def test_full_fidelity_produces_pptx_branch() -> None:
    """full → the model is told python-pptx IS available and shown how to save .pptx."""
    out = apply_docgen_fidelity(_docgen_spec(), full_fidelity=True)
    body = out.content
    assert "<!--fidelity:" not in body  # markers stripped
    # the full pptx branch is present …
    assert "python-pptx` IS available" in body
    assert "Presentation()" in body
    assert 'prs.save("/workspace/out/deck.pptx")' in body
    # … and the degrade branch is GONE (non-vacuous: the other variant is absent)
    assert "NOT available on this deployment" not in body


def test_degrade_offers_alternative_branch() -> None:
    """degrade → the model is told pptx is NOT available and to offer docx/md up-front."""
    out = apply_docgen_fidelity(_docgen_spec(), full_fidelity=False)
    body = out.content
    assert "<!--fidelity:" not in body
    # the degrade branch is present …
    assert "NOT available on this deployment" in body
    assert "offer a working alternative" in body
    # … and the full/produce branch is GONE
    assert "Presentation()" not in body
    assert "deck.pptx" not in body


def test_pdf_try_import_is_in_both_variants() -> None:
    """The pdf section is UNfenced — the template-agnostic try-import survives both."""
    for full in (True, False):
        body = apply_docgen_fidelity(_docgen_spec(), full_fidelity=full).content
        assert "SimpleDocTemplate" in body  # reportlab branch
        assert "except ModuleNotFoundError" in body  # matplotlib fallback branch
        assert "PdfPages" in body


def test_variants_actually_differ() -> None:
    """The two resolutions are genuinely different content (the flag does something)."""
    spec = _docgen_spec()
    full = apply_docgen_fidelity(spec, full_fidelity=True).content
    degrade = apply_docgen_fidelity(spec, full_fidelity=False).content
    assert full != degrade


def test_token_count_recomputed_for_delivered_variant() -> None:
    spec = _docgen_spec()
    out = apply_docgen_fidelity(spec, full_fidelity=True)
    # resolved content drops one branch + the markers ⇒ fewer tokens than the fenced source
    assert out.content_token_count < spec.content_token_count
    assert out.content_token_count > 0


def test_non_docgen_skill_passes_through_unchanged() -> None:
    spec = _docgen_spec()
    faux = spec.model_copy(update={"name": "some_other_skill"})
    out = apply_docgen_fidelity(faux, full_fidelity=True)
    assert out is faux  # untouched — the transform is scoped to document_generation


def test_resolve_markers_is_idempotent_on_resolved_content() -> None:
    resolved = resolve_fidelity_markers(_docgen_spec().content, full_fidelity=True)
    # no markers remain ⇒ a second pass is a no-op
    assert resolve_fidelity_markers(resolved, full_fidelity=False) == resolved


def test_neither_variant_teaches_a_runtime_install() -> None:
    """Egress/no-pip content guard (D-12-4): BOTH resolved variants — full and
    degrade — must be free of affirmative install commands. The full variant adds
    python-pptx usage; it must never add an install to obtain it (it's baked in)."""
    spec = _docgen_spec()
    for full in (True, False):
        body = apply_docgen_fidelity(spec, full_fidelity=full).content.lower()
        for forbidden in ("pip install", "!pip", "subprocess.run(['pip'", "apt-get install"):
            assert forbidden not in body, f"fidelity(full={full}) must not teach `{forbidden}`"
