"""``RuntimeFactory`` carries the P5 ``docgen_full_fidelity`` capability flag.

Spec P5 (P5-D-4): the composition root computes the flag from
``PERSONA_SANDBOX_TEMPLATE`` and passes it in; the factory stores it (consumed
when composing the ``document_generation`` skill). This guards the threading —
the flag reaches the factory and **defaults False** (the safe offline-degrade
path) so a caller that never sets it can't accidentally claim full fidelity.
"""

from __future__ import annotations

from pathlib import Path

from persona_api.services.runtime_factory import RuntimeFactory

BUILTIN_ROOT = Path(__file__).resolve().parents[4] / "packages"


def _factory(**kw: object) -> RuntimeFactory:
    # The flag is stored in __init__ and never touches the engine/registry, so
    # sentinel collaborators suffice for this wiring assertion (the pattern the
    # skills-wiring test uses).
    return RuntimeFactory(
        rls_engine=object(),  # type: ignore[arg-type]
        embedder=None,  # type: ignore[arg-type]
        tier_registry=None,  # type: ignore[arg-type]
        turn_log_writer=None,  # type: ignore[arg-type]
        audit_root=Path("/tmp"),
        **kw,  # type: ignore[arg-type]
    )


def test_defaults_to_degrade() -> None:
    """Omitted ⇒ False ⇒ the offline-degrade fallback (community/default safe)."""
    assert _factory()._docgen_full_fidelity is False  # noqa: SLF001


def test_flag_is_stored_when_set() -> None:
    assert _factory(docgen_full_fidelity=True)._docgen_full_fidelity is True  # noqa: SLF001


def _persona_with_docgen() -> object:
    from persona.schema.persona import Persona, PersonaIdentity

    return Persona(
        persona_id="p_doc",
        identity=PersonaIdentity(
            name="Doc Bot", role="Writer", background="Produces downloadable documents."
        ),
        tools=["code_execution"],
        skills=["document_generation"],
    )


def _scanned_docgen_content(*, full: bool) -> str:
    factory = _factory(docgen_full_fidelity=full)
    _scanner, scanned = factory._scan_skills(_persona_with_docgen())  # type: ignore[arg-type]  # noqa: SLF001
    docgen = next(s for s in scanned if s.name == "document_generation")  # type: ignore[attr-defined]
    return docgen.content  # type: ignore[attr-defined, no-any-return]


def test_scan_skills_delivers_full_pptx_when_template_active() -> None:
    """End-to-end wiring: docgen_full_fidelity=True ⇒ the delivered SKILL.md teaches
    producing real .pptx (python-pptx), markers resolved."""
    body = _scanned_docgen_content(full=True)
    assert "Presentation()" in body
    assert "<!--fidelity:" not in body
    assert "NOT available on this deployment" not in body


def test_scan_skills_delivers_degrade_pptx_by_default() -> None:
    """The community/default path: docgen_full_fidelity=False ⇒ the delivered SKILL.md
    offers docx/md up-front and never teaches producing .pptx."""
    body = _scanned_docgen_content(full=False)
    assert "NOT available on this deployment" in body
    assert "Presentation()" not in body
