"""Unit tests for ``SandboxTemplateConfig`` (Spec P5, P5-D-3 / P5-D-4).

Verifies the ``PERSONA_SANDBOX_TEMPLATE`` env wiring, the safe ``None`` default
(community/OSS keeps the SDK default template), blank-is-unset coercion (a blank
Fly secret must degrade, never select an empty template name), and the
``docgen_full_fidelity`` single-source-of-truth derivation.
"""

from __future__ import annotations

import pytest
from persona_api.sandbox.config import SandboxTemplateConfig


def test_default_is_unset_and_degraded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset ⇒ template None ⇒ SDK default ⇒ NOT full fidelity (P5-D-6 OSS default)."""
    monkeypatch.delenv("PERSONA_SANDBOX_TEMPLATE", raising=False)
    cfg = SandboxTemplateConfig()
    assert cfg.template is None
    assert cfg.docgen_full_fidelity is False


def test_custom_template_enables_full_fidelity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSONA_SANDBOX_TEMPLATE", "persona-docgen-interpreter")
    cfg = SandboxTemplateConfig()
    assert cfg.template == "persona-docgen-interpreter"
    assert cfg.docgen_full_fidelity is True


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_blank_secret_degrades_to_unset(blank: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """A blank/whitespace ``PERSONA_SANDBOX_TEMPLATE`` must behave exactly like unset —
    never select an empty template name (a mis-set Fly secret degrades safely)."""
    monkeypatch.setenv("PERSONA_SANDBOX_TEMPLATE", blank)
    cfg = SandboxTemplateConfig()
    assert cfg.template is None
    assert cfg.docgen_full_fidelity is False


def test_whitespace_around_alias_is_trimmed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSONA_SANDBOX_TEMPLATE", "  persona-docgen-interpreter  ")
    cfg = SandboxTemplateConfig()
    assert cfg.template == "persona-docgen-interpreter"
    assert cfg.docgen_full_fidelity is True
