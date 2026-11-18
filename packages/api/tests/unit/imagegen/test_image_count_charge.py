"""R9-198 through the whole request flow: the charge follows the images that came back.

``generate`` pre-deducts one ceiling per REQUESTED image, then trues up to the real cost.
The real cost of a registry-priced image is per image, so it must scale with the number
the provider actually RETURNED. These drive the real ``generate`` with a fake backend and a
recording ledger: no database, no provider, no network.
"""

# ruff: noqa: ANN401, ARG002 (the fakes mirror real keyword signatures)
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from typing import TYPE_CHECKING, Any

import pytest
from persona.billing import BillingConfig
from persona.imagegen import GeneratedImage, GenerationResult, ImageGenOptions
from persona_api.imagegen import service as imagegen_service
from persona_api.storage import LocalFileStorage

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_TINY_PNG: bytes = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
    "0000000c49444154789c63f8cfc0000003010100c9fe92ef0000000049454e44ae"
    "426082"
)
_CEILING = 50


class _Backend:
    """An OpenAI-shaped backend that reports no cost and returns ``returns`` images."""

    def __init__(self, *, returns: int) -> None:
        self._returns = returns

    @property
    def provider_name(self) -> str:
        return "openai"

    @property
    def model_name(self) -> str:
        return "gpt-image-1"

    async def generate(self, prompt: str, *, options: ImageGenOptions) -> GenerationResult:
        image = GeneratedImage(image_bytes=_TINY_PNG, media_type="image/png", width=1, height=1)
        return GenerationResult(
            images=[image] * self._returns,
            provider="openai",
            model="gpt-image-1",
            latency_ms=1.0,
        )


class _Ledger:
    """Records every credits call the flow makes, in order."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def deduct(self, **kw: Any) -> int:
        self.calls.append(("deduct", kw))
        return 0

    def capture_up_to(self, **kw: Any) -> tuple[int, int]:
        self.calls.append(("capture_up_to", kw))
        return int(kw["amount"]), 0

    def refund(self, **kw: Any) -> int:
        self.calls.append(("refund", kw))
        return 0


class _Engine:
    """Just enough of an Engine for the cap transaction; nothing reaches a database."""

    def begin(self) -> Any:
        return nullcontext(object())


@contextmanager
def _always_acquired(**_: Any) -> Iterator[bool]:
    yield True


async def _run(tmp_path: Path, *, requested: int, returned: int) -> _Ledger:
    ledger = _Ledger()
    await imagegen_service.generate(
        rls_engine=_Engine(),  # type: ignore[arg-type]
        credits_policy=ledger,  # type: ignore[arg-type]
        file_storage=LocalFileStorage(tmp_path / "ws"),
        backend=_Backend(returns=returned),  # type: ignore[arg-type]
        user_id="u_count",
        persona_id="p_count",
        persona_visual_style=None,
        prompt="a lighthouse at dusk",
        options=ImageGenOptions(count=requested),
        billing_config=BillingConfig(),
        ceiling_per_image=_CEILING,
        floor=1,
    )
    return ledger


@pytest.fixture(autouse=True)
def _no_advisory_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(imagegen_service, "acquire_user_concurrency", _always_acquired)


@pytest.mark.asyncio
async def test_two_images_returned_cost_twice_one(tmp_path: Path) -> None:
    one = await _run(tmp_path, requested=1, returned=1)
    two = await _run(tmp_path, requested=2, returned=2)

    one_kw = one.calls[-1][1]
    two_kw = two.calls[-1][1]
    assert one_kw["cost_cents"] == pytest.approx(4.2)
    assert two_kw["cost_cents"] == pytest.approx(8.4), "a two-image call billed one image"
    # Two ceilings pre-deducted (100), 9 credits real: 91 back.
    assert two.calls[0] == ("deduct", two.calls[0][1])
    assert two.calls[0][1]["amount"] == 2 * _CEILING
    assert two.calls[-1][0] == "refund"
    assert two_kw["amount"] == 91


@pytest.mark.asyncio
async def test_two_asked_one_returned_is_charged_for_one(tmp_path: Path) -> None:
    """The provider sent back fewer than were asked for. The person pays for what they got."""
    ledger = await _run(tmp_path, requested=2, returned=1)

    assert ledger.calls[0][1]["amount"] == 2 * _CEILING, "the ceiling covers what was ASKED"
    method, kw = ledger.calls[-1]
    assert method == "refund"
    assert kw["cost_cents"] == pytest.approx(4.2), "charged for an image that never came back"
    assert kw["amount"] == 95
