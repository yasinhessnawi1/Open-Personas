"""The queue's avatar generator bills the owner through the same seam the request path uses.

The request path charges the persona owner after a successful generation
(``credits_policy.deduct_idempotent`` keyed ``avatar:{persona_id}``, Spec M3
D-M3-11). The durable path used to return ``cost_micros=0`` and never touched
the ledger, so moving generation to the worker would have made avatars free by
accident. Both paths now call one ``bill_avatar_owner`` and the generator
reports the charged cost to the job meter.
"""

# ruff: noqa: ANN401, ARG001, ARG002, ARG005 — the fakes mirror real keyword signatures
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from persona.imagegen import ContentRejectedError, GeneratedImage, GenerationResult
from persona_api.imagegen import service as imagegen_service
from persona_api.imagegen.service import ImagegenAvatarGenerator
from persona_api.services import avatar_billing, persona_service

_OWNER = "u_bill"
_PERSONA = "persona_bill"


def _generation(*, cost_usd: float | None = 0.02) -> GenerationResult:
    return GenerationResult(
        images=[
            GeneratedImage(
                image_bytes=b"\x89PNG",
                workspace_path="uploads/abc.png",
                media_type="image/png",
                width=1,
                height=1,
                revised_prompt=None,
            )
        ],
        provider="openrouter",
        model="openai/gpt-5.4-image-2",
        latency_ms=5.0,
        prompt_tokens=10,
        completion_tokens=100,
        cost_usd=cost_usd,
    )


class _Ledger:
    """Records the idempotent deduct the way the credits policy would receive it."""

    def __init__(self) -> None:
        self.deducts: list[dict[str, Any]] = []

    def deduct_idempotent(self, **kwargs: Any) -> int:
        self.deducts.append(kwargs)
        return int(kwargs["amount"])


@pytest.fixture
def scripted_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the provider + persist internals; the seam under test is what happens after."""
    monkeypatch.setattr(
        persona_service,
        "load_persona_from_yaml",
        lambda yaml_str, **_: SimpleNamespace(identity=SimpleNamespace()),
    )
    monkeypatch.setattr(imagegen_service, "craft_avatar_prompt", lambda identity: "a portrait")

    async def _generate_avatar(**_: Any) -> GenerationResult:
        return _generation()

    monkeypatch.setattr(imagegen_service, "generate_avatar", _generate_avatar)


def _generator(ledger: _Ledger | None) -> ImagegenAvatarGenerator:
    return ImagegenAvatarGenerator(
        backend=object(),  # type: ignore[arg-type]
        file_storage=object(),  # type: ignore[arg-type]
        audit_logger=None,
        timeout_s=5.0,
        credits_policy=ledger,  # type: ignore[arg-type]
        rls_engine=object(),  # type: ignore[arg-type]
        cost_source=None,
        image_credit_floor=1,
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("scripted_generation")
async def test_success_bills_the_owner_under_the_given_key_and_reports_the_cost() -> None:
    ledger = _Ledger()

    result = await _generator(ledger).generate(
        persona_id=_PERSONA,
        owner_id=_OWNER,
        yaml_str="identity: {}",
        billing_key=f"avatar:{_PERSONA}",
    )

    assert result is not None
    assert result.avatar_url == "uploads/abc.png"
    assert result.provider == "openrouter"
    assert len(ledger.deducts) == 1
    deduct = ledger.deducts[0]
    assert deduct["user_id"] == _OWNER
    assert deduct["billing_key"] == f"avatar:{_PERSONA}"
    assert deduct["reason"].startswith("avatar_gen:")
    # The OpenRouter actual ($0.02) is the authoritative cost: 2 cents.
    assert deduct["cost_cents"] == pytest.approx(2.0)
    assert deduct["amount"] >= 1
    # The meter sees the same cost in ledger micros (a hundredth of a cent).
    assert result.cost_micros == 200


@pytest.mark.asyncio
@pytest.mark.usefixtures("scripted_generation")
async def test_regen_key_reaches_the_ledger_unchanged() -> None:
    ledger = _Ledger()
    await _generator(ledger).generate(
        persona_id=_PERSONA,
        owner_id=_OWNER,
        yaml_str="identity: {}",
        billing_key=f"avatar:{_PERSONA}:regen:t1",
    )
    assert ledger.deducts[0]["billing_key"] == f"avatar:{_PERSONA}:regen:t1"


@pytest.mark.asyncio
@pytest.mark.usefixtures("scripted_generation")
async def test_no_billing_seam_composed_means_no_charge_and_zero_cost() -> None:
    """A worker without a credits policy still generates; it just cannot bill."""
    result = await _generator(None).generate(
        persona_id=_PERSONA, owner_id=_OWNER, yaml_str="identity: {}", billing_key="avatar:x"
    )
    assert result is not None
    assert result.cost_micros == 0


@pytest.mark.asyncio
async def test_content_rejection_is_a_decline_not_a_charge(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        persona_service,
        "load_persona_from_yaml",
        lambda yaml_str, **_: SimpleNamespace(identity=SimpleNamespace()),
    )
    monkeypatch.setattr(imagegen_service, "craft_avatar_prompt", lambda identity: "a portrait")

    async def _rejected(**_: Any) -> GenerationResult:
        raise ContentRejectedError("no", context={"reason": "hard_line_categorical"})

    monkeypatch.setattr(imagegen_service, "generate_avatar", _rejected)
    ledger = _Ledger()

    result = await _generator(ledger).generate(
        persona_id=_PERSONA, owner_id=_OWNER, yaml_str="identity: {}", billing_key="avatar:x"
    )

    assert result is None
    assert ledger.deducts == []


def test_bill_avatar_owner_never_raises_when_the_ledger_fails() -> None:
    class _Broken:
        def deduct_idempotent(self, **_: Any) -> int:
            raise RuntimeError("ledger down")

    charge = avatar_billing.bill_avatar_owner(
        credits_policy=_Broken(),  # type: ignore[arg-type]
        rls_engine=object(),  # type: ignore[arg-type]
        cost_source=None,
        image_credit_floor=1,
        owner_id=_OWNER,
        persona_id=_PERSONA,
        result=_generation(),
        billing_key="avatar:x",
    )
    assert charge is None
