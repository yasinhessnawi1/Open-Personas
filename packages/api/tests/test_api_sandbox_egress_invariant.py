"""Spec P5 A3c — the egress-OFF / no-runtime-pip invariant survives the custom template.

D-12-4 is the security control P5 exists to preserve: libs are baked into the template
at BUILD time so the runtime never needs egress. These tests assert the INVARIANT
STRUCTURALLY — that no sandbox-creation path enables internet — not merely that a flag
is False. They capture the actual kwargs handed to the E2B SDK and assert
``allow_internet_access=False`` holds **with and without** a custom template, and that a
custom template is never a route to egress. A future regression that flips internet on
(or drops the deny) fails here.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from persona.sandbox.result import NetworkPolicy, ResourceLimits
from persona_api.sandbox import HostedSandbox


def _capture_create_kwargs(sandbox: HostedSandbox, network: NetworkPolicy) -> dict[str, Any]:
    """Run ``_create_sandbox`` against a mocked E2B SDK; return the create kwargs."""
    fake_ctor = MagicMock(return_value=MagicMock())
    with patch("e2b_code_interpreter.Sandbox", fake_ctor):
        sandbox._create_sandbox(  # noqa: SLF001 — exercising the create-kwargs path
            limits=ResourceLimits(),
            network=network,
        )
    assert fake_ctor.call_count == 1
    return fake_ctor.call_args.kwargs


def test_egress_off_with_custom_template() -> None:
    """Custom template + default (egress-off) policy ⇒ allow_internet_access=False,
    and the template alias IS passed — i.e. the template selects the image, never egress."""
    kwargs = _capture_create_kwargs(
        HostedSandbox(template="persona-docgen-interpreter"), NetworkPolicy()
    )
    assert kwargs["allow_internet_access"] is False
    assert kwargs["template"] == "persona-docgen-interpreter"


def test_egress_off_without_template() -> None:
    """Default template (community/OSS) ⇒ egress still off; no template kwarg leaks."""
    kwargs = _capture_create_kwargs(HostedSandbox(), NetworkPolicy())
    assert kwargs["allow_internet_access"] is False
    assert "template" not in kwargs  # None ⇒ SDK default, not an empty selection


def test_template_choice_does_not_change_egress() -> None:
    """The structural invariant: egress is identical (off) regardless of template —
    the template is not, and cannot become, a path to internet access."""
    with_tpl = _capture_create_kwargs(
        HostedSandbox(template="persona-docgen-interpreter"), NetworkPolicy()
    )
    without = _capture_create_kwargs(HostedSandbox(), NetworkPolicy())
    assert with_tpl["allow_internet_access"] == without["allow_internet_access"] is False


def test_create_never_enables_internet() -> None:
    """No create path sets allow_internet_access=True. With egress off it is explicitly
    False; the SDK default (no key present) is off too — the kwarg is never True."""
    kwargs = _capture_create_kwargs(
        HostedSandbox(template="persona-docgen-interpreter"), NetworkPolicy()
    )
    assert kwargs.get("allow_internet_access", False) is not True
