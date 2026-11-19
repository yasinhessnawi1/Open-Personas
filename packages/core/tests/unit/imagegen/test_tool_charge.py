"""The ``generate_image`` tool's charge seam and its count cap (R9-308).

Until R9-308 the chat tool called the backend directly, so an image a persona made in a
chat turn was never charged. Core does not bill (it may not import the api's credits
policy), so the tool takes an injected :class:`ImageChargeHook`: it is asked BEFORE the
provider call whether the image may be made, and the charge it hands back is settled with
the provider's result AFTER a success. These tests pin that ordering with a recording
hook; the pricing and the ledger are the api's and are tested there.

The count cap is here too: the tool used to accept up to four images per call while the
HTTP route allows two, so a chat turn could make twice what any other surface may.
"""

from __future__ import annotations

import pytest
from persona.imagegen import (
    MAX_IMAGES_PER_CALL,
    GeneratedImage,
    GenerationResult,
    ImageBackend,
    ImageChargeLedgerError,
    ImageChargeRefusedError,
    ImageGenOptions,
    ImageProviderError,
    make_generate_image_tool,
)
from persona.imagegen import tool as tool_module
from persona.tools import MemoryToolAuditLogger


class _Backend:
    """Returns one image per requested image, or raises ``side_effect``."""

    provider_name = "openai"
    model_name = "gpt-image-1"

    def __init__(self, *, side_effect: BaseException | None = None) -> None:
        self._side_effect = side_effect
        self.calls: list[ImageGenOptions] = []

    async def generate(
        self,
        prompt: str,  # noqa: ARG002
        *,
        options: ImageGenOptions | None = None,
    ) -> GenerationResult:
        options = options or ImageGenOptions()
        self.calls.append(options)
        if self._side_effect is not None:
            raise self._side_effect
        image = GeneratedImage(image_bytes=b"\x89PNG", media_type="image/png", width=1, height=1)
        return GenerationResult(
            images=[image] * options.count,
            provider=self.provider_name,
            model=self.model_name,
            latency_ms=1.0,
        )

    async def edit(
        self,
        input_image: GeneratedImage,
        instructions: str,
        *,
        options: ImageGenOptions | None = None,
    ) -> GenerationResult:
        return await ImageBackend.edit(self, input_image, instructions, options=options)


class _Charge:
    def __init__(self, log: list[str], *, fail: BaseException | None = None) -> None:
        self._log = log
        self._fail = fail
        self.settled: list[GenerationResult] = []
        self.released = 0

    async def settle(self, result: GenerationResult) -> None:
        self._log.append("settle")
        self.settled.append(result)
        if self._fail is not None:
            raise self._fail

    async def release(self) -> None:
        self._log.append("release")
        self.released += 1


class _Hook:
    """Records the order of authorize / generate / settle."""

    def __init__(
        self,
        log: list[str],
        *,
        refuse: str | None = None,
        no_charge: bool = False,
        settle_fails: BaseException | None = None,
    ) -> None:
        self._log = log
        self._refuse = refuse
        self._no_charge = no_charge
        self.authorized: list[tuple[ImageGenOptions, str, str]] = []
        self.charge = _Charge(log, fail=settle_fails)

    async def authorize(
        self, options: ImageGenOptions, *, provider: str, model: str
    ) -> _Charge | None:
        self._log.append("authorize")
        self.authorized.append((options, provider, model))
        if self._refuse is not None:
            msg = "Not enough credits for this image."
            raise ImageChargeRefusedError(msg, context={"reason": self._refuse})
        return None if self._no_charge else self.charge


class _LoggingBackend(_Backend):
    def __init__(self, log: list[str], **kwargs: BaseException | None) -> None:
        super().__init__(**kwargs)
        self._log = log

    async def generate(
        self, prompt: str, *, options: ImageGenOptions | None = None
    ) -> GenerationResult:
        self._log.append("generate")
        return await super().generate(prompt, options=options)


@pytest.mark.asyncio
async def test_authorized_before_the_provider_and_settled_after_it() -> None:
    log: list[str] = []
    hook = _Hook(log)
    backend = _LoggingBackend(log)
    tool = make_generate_image_tool(backend=backend, charge_hook=hook)

    result = await tool.execute(prompt="a lighthouse at dusk", count=2)

    assert result.is_error is False
    assert log == ["authorize", "generate", "settle"]
    options, provider, model = hook.authorized[0]
    assert options.count == 2
    assert (provider, model) == ("openai", "gpt-image-1")
    assert len(hook.charge.settled) == 1
    assert len(hook.charge.settled[0].images) == 2, "settled with what the provider returned"


@pytest.mark.asyncio
async def test_an_unaffordable_image_never_reaches_the_provider() -> None:
    log: list[str] = []
    audit = MemoryToolAuditLogger()
    backend = _LoggingBackend(log)
    tool = make_generate_image_tool(
        backend=backend, charge_hook=_Hook(log, refuse="insufficient_credits"), audit_logger=audit
    )

    result = await tool.execute(prompt="a lighthouse at dusk")

    assert result.is_error is True
    assert backend.calls == [], "the provider was called for an image nobody can pay for"
    assert log == ["authorize"]
    assert result.metadata["reason"] == "insufficient_credits"
    assert "Not enough credits" in result.content
    events = audit.events
    assert len(events) == 1
    assert events[0].metadata["reason"] == "insufficient_credits"


@pytest.mark.asyncio
async def test_a_provider_failure_settles_nothing() -> None:
    log: list[str] = []
    hook = _Hook(log)
    backend = _LoggingBackend(
        log, side_effect=ImageProviderError("upstream 503", context={"reason": "transient"})
    )
    tool = make_generate_image_tool(backend=backend, charge_hook=hook)

    result = await tool.execute(prompt="a lighthouse at dusk")

    assert result.is_error is True
    assert log == ["authorize", "generate", "release"]
    assert hook.charge.settled == []
    assert hook.charge.released == 1


@pytest.mark.asyncio
async def test_an_unexpected_provider_exception_also_releases() -> None:
    """Not every failure is an ImageGenError; a held authorization must come back anyway."""
    log: list[str] = []
    hook = _Hook(log)
    backend = _LoggingBackend(log, side_effect=RuntimeError("socket closed"))
    tool = make_generate_image_tool(backend=backend, charge_hook=hook)

    result = await tool.execute(prompt="a lighthouse at dusk")

    assert result.is_error is True
    assert log == ["authorize", "generate", "release"]
    assert hook.charge.settled == []


@pytest.mark.asyncio
async def test_the_refusal_reason_comes_from_the_hook() -> None:
    log: list[str] = []
    backend = _LoggingBackend(log)
    tool = make_generate_image_tool(
        backend=backend, charge_hook=_Hook(log, refuse="daily_spend_cap")
    )

    result = await tool.execute(prompt="a lighthouse at dusk")

    assert result.is_error is True
    assert result.metadata["reason"] == "daily_spend_cap"
    assert result.content.startswith("daily_spend_cap: ")
    assert backend.calls == []


@pytest.mark.asyncio
async def test_no_charge_handed_back_means_nothing_to_settle() -> None:
    """The hook may decline to bill (no request owner, the CLI); the image still works."""
    log: list[str] = []
    tool = make_generate_image_tool(
        backend=_LoggingBackend(log), charge_hook=_Hook(log, no_charge=True)
    )

    result = await tool.execute(prompt="a lighthouse at dusk")

    assert result.is_error is False
    assert log == ["authorize", "generate"]


@pytest.mark.asyncio
async def test_a_ledger_failure_on_settle_does_not_take_the_image_away() -> None:
    """The image exists and the provider billed us; a ledger failure is logged, not raised."""
    log: list[str] = []
    failure = ImageChargeLedgerError("ledger unavailable", context={"billing_key": "k"})
    tool = make_generate_image_tool(
        backend=_LoggingBackend(log), charge_hook=_Hook(log, settle_fails=failure)
    )

    result = await tool.execute(prompt="a lighthouse at dusk")

    assert result.is_error is False
    assert log == ["authorize", "generate", "settle"]


class _RecordingLog:
    """Stands in for the tool's logger so a test can read the level of what it said."""

    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def warning(self, message: str, **_: object) -> None:
        self.records.append(("warning", message))

    def error(self, message: str, **_: object) -> None:
        self.records.append(("error", message))

    def info(self, message: str, **_: object) -> None:
        self.records.append(("info", message))


@pytest.mark.asyncio
async def test_a_bug_in_settle_keeps_the_image_and_is_logged_as_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The person has paid the hold; a bug must not also take the image. It stays visible."""
    recorder = _RecordingLog()
    monkeypatch.setattr(tool_module, "_LOG", recorder)
    log: list[str] = []
    tool = make_generate_image_tool(
        backend=_LoggingBackend(log), charge_hook=_Hook(log, settle_fails=TypeError("bug"))
    )

    result = await tool.execute(prompt="a lighthouse at dusk")

    assert result.is_error is False, "a bug in settle took the image away"
    assert [level for level, _ in recorder.records] == ["error"]


@pytest.mark.asyncio
async def test_a_ledger_failure_on_settle_is_a_warning_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _RecordingLog()
    monkeypatch.setattr(tool_module, "_LOG", recorder)
    log: list[str] = []
    failure = ImageChargeLedgerError("ledger unavailable")
    tool = make_generate_image_tool(
        backend=_LoggingBackend(log), charge_hook=_Hook(log, settle_fails=failure)
    )

    await tool.execute(prompt="a lighthouse at dusk")

    assert [level for level, _ in recorder.records] == ["warning"]


@pytest.mark.asyncio
async def test_a_hard_line_refusal_never_asks_to_charge() -> None:
    log: list[str] = []
    hook = _Hook(log)
    tool = make_generate_image_tool(backend=_LoggingBackend(log), charge_hook=hook)

    # Built in the body, as test_tool.py does, so no standalone literal is committed.
    trigger = " ".join(["a", "nude", "child"])

    result = await tool.execute(prompt=trigger)

    assert result.metadata["outcome"] == "content_rejected_hard_line"
    assert log == []


@pytest.mark.asyncio
async def test_without_a_hook_the_tool_behaves_as_before() -> None:
    backend = _Backend()
    tool = make_generate_image_tool(backend=backend)

    result = await tool.execute(prompt="a lighthouse at dusk")

    assert result.is_error is False
    assert len(backend.calls) == 1


def test_the_chat_cap_is_the_route_cap() -> None:
    assert MAX_IMAGES_PER_CALL == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [3, 4])
async def test_a_count_above_the_cap_is_refused_before_anything(count: int) -> None:
    log: list[str] = []
    backend = _LoggingBackend(log)
    tool = make_generate_image_tool(backend=backend, charge_hook=_Hook(log))

    result = await tool.execute(prompt="a lighthouse at dusk", count=count)

    assert result.is_error is True
    assert result.metadata["reason"] == "invalid_options"
    assert log == [], "neither the charge nor the provider may see a count above the cap"


@pytest.mark.asyncio
async def test_the_cap_itself_is_allowed() -> None:
    backend = _Backend()
    tool = make_generate_image_tool(backend=backend)

    result = await tool.execute(prompt="a lighthouse at dusk", count=MAX_IMAGES_PER_CALL)

    assert result.is_error is False
    assert backend.calls[0].count == MAX_IMAGES_PER_CALL
