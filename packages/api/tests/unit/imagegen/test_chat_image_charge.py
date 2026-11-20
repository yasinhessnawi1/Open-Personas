"""R9-308: an image a persona makes in chat is charged to the owner, once, at what it cost.

The chat ``generate_image`` tool used to call the backend directly, so every image made in
a chat turn cost the house the provider price and the person nothing. The api now injects
:class:`ChatImageChargeHook` into the tool. Before the provider is called it HOLDS the
estimate with a keyed ``deduct_idempotent``, which checks the balance and books the daily
spend cap in one step and stops two calls spending the same balance. After a success it
trues the hold up to what the images cost, priced through the same
``generation_cost_cents`` as the request path. A failed generation gives the hold back.

Everything here runs against a recording ledger that honours billing keys and an in-memory
backend: no database, no provider, no network, no Stripe.
"""

# ruff: noqa: ANN401, ARG002 (the fakes mirror real keyword signatures)
from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from persona.backends.types import StreamChunk
from persona.billing import BillingConfig, credits_charged
from persona.errors import CreditsExhaustedError, DailySpendCapExceededError
from persona.imagegen import (
    GeneratedImage,
    GenerationResult,
    ImageBackend,
    ImageChargeLedgerError,
    ImageChargeRefusedError,
    ImageGenOptions,
    ImageProviderError,
    make_generate_image_tool,
)
from persona.schema.conversation import Conversation
from persona.schema.persona import Persona, PersonaIdentity
from persona.schema.tools import ToolCall
from persona_api.background.chat_turn_worker import ChatTurnRegistry
from persona_api.config import APIConfig, Edition
from persona_api.editions import UnlimitedCreditsPolicy
from persona_api.imagegen import chat_charge as chat_charge_module
from persona_api.imagegen.chat_charge import CHAT_IMAGE_REASON, ChatImageChargeHook
from persona_api.sandbox import (
    SandboxRequestContext,
    reset_sandbox_request_context,
    set_sandbox_request_context,
)
from persona_api.services import audit_service
from persona_api.services.runtime_factory import RuntimeFactory

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterator

    from persona.tools.protocol import AsyncTool
    from persona_runtime.agentic.events import RunEvent

_OWNER = "u_chatimg"
_CONV = "c_chatimg"
_PERSONA = "p_chatimg"
#: OpenAI gpt-image-1's registry row: 4.2 cents per image (R9-198's figure).
_ONE_IMAGE_CENTS = 4.2
_ONE_IMAGE = credits_charged(
    provider_cents=_ONE_IMAGE_CENTS, infra_flat_cents=0.0, markup=1.0, floor=1
)
_TWO_IMAGES = credits_charged(
    provider_cents=2 * _ONE_IMAGE_CENTS, infra_flat_cents=0.0, markup=1.0, floor=1
)


class _Backend:
    """OpenAI-shaped: reports no cost, so the registry's per-image price applies."""

    provider_name = "openai"
    model_name = "gpt-image-1"

    def __init__(
        self, *, side_effect: BaseException | None = None, returns: int | None = None
    ) -> None:
        self._side_effect = side_effect
        self._returns = returns
        self.calls: list[ImageGenOptions] = []

    async def generate(
        self, prompt: str, *, options: ImageGenOptions | None = None
    ) -> GenerationResult:
        options = options or ImageGenOptions()
        self.calls.append(options)
        if self._side_effect is not None:
            raise self._side_effect
        image = GeneratedImage(image_bytes=b"\x89PNG", media_type="image/png", width=1, height=1)
        return GenerationResult(
            images=[image] * (self._returns or options.count),
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


class _Ledger:
    """A credits policy double with a balance and a day cap, honouring billing keys.

    The keyed methods mirror the real ``ON CONFLICT (billing_key) DO NOTHING`` gate: a key
    seen before moves nothing. ``drop_after_commit`` names methods whose NEXT call lands
    and then raises, the shape of a connection dropped after the commit; ``broken`` names
    methods that always fail before writing anything.
    """

    def __init__(self, *, balance: int = 1_000, day_cap: int = 0, day_spent: int = 0) -> None:
        self.balance = balance
        self.day_cap = day_cap
        self.day_spent = day_spent
        self.rows: list[dict[str, Any]] = []
        self.keys: set[str] = set()
        self.attempts: dict[str, int] = {}
        self.drop_after_commit: set[str] = set()
        #: Methods whose next call lands, raises, and leaves the method broken after it.
        self.drop_then_break: set[str] = set()
        self.broken: set[str] = set()
        #: Methods that wait on ``gate`` before writing; ``entered`` is set on arrival.
        self.gated: set[str] = set()
        self.gate = threading.Event()
        self.entered = threading.Event()

    # -- helpers -----------------------------------------------------------------
    def _enter(self, method: str) -> None:
        self.attempts[method] = self.attempts.get(method, 0) + 1
        if method in self.broken:
            msg = f"{method}: database unavailable"
            raise ConnectionError(msg)
        if method in self.gated:
            self.entered.set()
            assert self.gate.wait(timeout=10), "the test never opened the gate"

    def _leave(self, method: str) -> None:
        if method in self.drop_then_break:
            self.drop_then_break.discard(method)
            self.broken.add(method)
            msg = f"{method}: connection dropped after commit, and stays down"
            raise ConnectionError(msg)
        if method in self.drop_after_commit:
            self.drop_after_commit.discard(method)
            msg = f"{method}: connection dropped after commit"
            raise ConnectionError(msg)

    def _book(self, amount: int) -> None:
        if self.day_cap > 0 and self.day_spent + amount > self.day_cap:
            raise DailySpendCapExceededError(
                "Daily spend cap reached. This resets at UTC midnight.",
                context={"cap": str(self.day_cap), "spent": str(self.day_spent)},
            )
        self.day_spent += amount

    def _row(self, method: str, delta: int, kw: dict[str, Any]) -> None:
        self.balance += delta
        self.rows.append({**kw, "method": method, "delta": delta})

    @property
    def net_charged(self) -> int:
        return -sum(int(r["delta"]) for r in self.rows)

    def image_rows(self) -> list[dict[str, Any]]:
        return [r for r in self.rows if str(r["reason"]).startswith(CHAT_IMAGE_REASON)]

    @property
    def image_charged(self) -> int:
        return -sum(int(r["delta"]) for r in self.image_rows())

    # -- the policy surface --------------------------------------------------------
    def deduct(self, **kw: Any) -> int:
        if int(kw["amount"]) > self.balance:
            raise CreditsExhaustedError(context={"balance": str(self.balance)})
        self._row("deduct", -int(kw["amount"]), kw)
        return self.balance

    def capture_up_to(self, **kw: Any) -> tuple[int, int]:
        taken = min(int(kw["amount"]), self.balance)
        self._row("capture_up_to", -taken, kw)
        return taken, self.balance

    def deduct_idempotent(self, **kw: Any) -> int:
        self._enter("deduct_idempotent")
        if kw["billing_key"] in self.keys:
            return self.balance
        amount = int(kw["amount"])
        self._book(amount)
        if amount > self.balance:
            self.day_spent -= amount  # the real transaction rolls the booking back
            raise CreditsExhaustedError(context={"balance": str(self.balance)})
        self.keys.add(kw["billing_key"])
        self._row("deduct_idempotent", -amount, kw)
        self._leave("deduct_idempotent")
        return self.balance

    def capture_up_to_idempotent(self, **kw: Any) -> tuple[int, int]:
        self._enter("capture_up_to_idempotent")
        if kw["billing_key"] in self.keys:
            return 0, self.balance
        taken = min(int(kw["amount"]), self.balance)
        self._book(taken)
        self.keys.add(kw["billing_key"])
        self._row("capture_up_to_idempotent", -taken, kw)
        self._leave("capture_up_to_idempotent")
        return taken, self.balance

    def grant_idempotent(self, **kw: Any) -> int:
        self._enter("grant_idempotent")
        if kw["billing_key"] in self.keys:
            return self.balance
        self.keys.add(kw["billing_key"])
        self._row("grant_idempotent", int(kw["amount"]), kw)
        self._leave("grant_idempotent")
        return self.balance


def _hook(ledger: Any, *, floor: int = 1) -> ChatImageChargeHook:
    return ChatImageChargeHook(
        credits_policy=ledger,
        rls_engine=object(),  # type: ignore[arg-type]  # the double ignores it
        cost_source=None,
        billing_config=BillingConfig(),
        floor=floor,
    )


def _tool(ledger: Any, backend: _Backend | None = None) -> AsyncTool:
    return make_generate_image_tool(
        backend=backend or _Backend(), persona_id=_PERSONA, charge_hook=_hook(ledger)
    )


def _bind(*, delivers_images: bool = True) -> Any:
    return set_sandbox_request_context(
        SandboxRequestContext(
            owner_id=_OWNER, conversation_id=_CONV, delivers_images=delivers_images
        )
    )


@pytest.fixture
def owner_context() -> Iterator[None]:
    """The chat worker binds this around every turn; the hook reads the owner from it."""
    token = _bind()
    try:
        yield
    finally:
        reset_sandbox_request_context(token)


@pytest.fixture
def audit_rows(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture the durable audit rows the hook writes, instead of reaching a database."""
    rows: list[dict[str, Any]] = []

    def _record(**kw: Any) -> None:
        rows.append(kw)

    monkeypatch.setattr(audit_service, "record", _record)
    return rows


# ---------------------------------------------------------------------------
# Through the real chat turn worker
# ---------------------------------------------------------------------------


class _ImageTurnLoop:
    """A scripted chat loop whose turn optionally calls ``generate_image``."""

    def __init__(self, tool: AsyncTool | None, *, count: int = 1) -> None:
        self._tool = tool
        self._count = count
        self.last_turn_cost_cents = 0.6
        self.last_turn_cost_basis = "actual_openrouter"

    async def turn(
        self,
        conversation: Conversation,
        user_message: str,
        on_event: Callable[[RunEvent], Awaitable[None]] | None = None,
        **_kwargs: object,
    ) -> AsyncIterator[StreamChunk]:
        if self._tool is not None:
            result = await self._tool.execute(prompt="a lighthouse at dusk", count=self._count)
            assert result.is_error is False, result.content
        yield StreamChunk(delta="here it is", is_final=True)


class _Sink:
    def checkpoint(self, **kwargs: object) -> None:
        return None

    def finalize(self, **kwargs: object) -> None:
        return None


async def _run_turn(
    ledger: _Ledger,
    *,
    with_image: bool,
    count: int = 1,
    backend: _Backend | None = None,
    delivers_images: bool = True,
) -> None:
    registry = ChatTurnRegistry(
        sink=_Sink(),
        rls_engine=object(),  # type: ignore[arg-type]
        credits_policy=ledger,  # type: ignore[arg-type]
        delivers_images=delivers_images,
    )
    loop = _ImageTurnLoop(_tool(ledger, backend) if with_image else None, count=count)
    handle = registry.start(
        conversation_id=_CONV,
        owner_id=_OWNER,
        assistant_message_id="m_chatimg",
        loop=loop,  # type: ignore[arg-type]
        conversation=Conversation(conversation_id=_CONV, persona_id=_PERSONA, messages=[]),
        user_message="draw me a lighthouse",
    )
    assert handle.task is not None
    await handle.task


@pytest.mark.asyncio
async def test_a_turn_that_made_an_image_costs_one_image_more() -> None:
    without = _Ledger()
    with_image = _Ledger()

    await _run_turn(without, with_image=False)
    await _run_turn(with_image, with_image=True)

    assert with_image.net_charged - without.net_charged == _ONE_IMAGE
    assert {r["user_id"] for r in with_image.image_rows()} == {_OWNER}
    settled = [r for r in with_image.image_rows() if r.get("cost_cents") is not None]
    assert len(settled) == 1, "exactly one row records what the image cost"
    assert settled[0]["cost_cents"] == pytest.approx(_ONE_IMAGE_CENTS)
    assert settled[0]["cost_basis"] != "unpriced"
    assert without.image_rows() == []


@pytest.mark.asyncio
async def test_two_images_in_a_turn_cost_twice_one() -> None:
    one = _Ledger()
    two = _Ledger()

    await _run_turn(one, with_image=True, count=1)
    await _run_turn(two, with_image=True, count=2)

    one_cost = next(r["cost_cents"] for r in one.image_rows() if r.get("cost_cents"))
    two_cost = next(r["cost_cents"] for r in two.image_rows() if r.get("cost_cents"))
    assert two_cost == pytest.approx(2 * one_cost)
    assert two.image_charged == _TWO_IMAGES


@pytest.mark.asyncio
async def test_a_connector_turn_makes_the_image_and_charges_nothing_for_it() -> None:
    """M1: a connector sends only text, so its turns never pay for an undelivered image."""
    connector = _Ledger()
    web = _Ledger()
    backend = _Backend()

    await _run_turn(connector, with_image=True, backend=backend, delivers_images=False)
    await _run_turn(web, with_image=False)

    assert len(backend.calls) == 1, "the image is still made, as before"
    assert connector.image_rows() == []
    assert connector.net_charged == web.net_charged, "the turn itself is billed as usual"


def test_a_registry_delivers_images_unless_told_otherwise() -> None:
    registry = ChatTurnRegistry(sink=_Sink(), credits_policy=None)
    assert registry.delivers_images is True


# ---------------------------------------------------------------------------
# Before the provider: the hold
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("owner_context")
async def test_an_unaffordable_image_does_not_call_the_provider() -> None:
    ledger = _Ledger(balance=_ONE_IMAGE - 2)
    backend = _Backend()

    result = await _tool(ledger, backend).execute(prompt="a lighthouse at dusk")

    assert result.is_error is True
    assert backend.calls == [], "the provider was called for an image the owner cannot pay for"
    assert result.metadata["reason"] == "insufficient_credits"
    assert "credits" in result.content
    assert ledger.rows == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("owner_context")
async def test_an_empty_wallet_does_not_call_the_provider() -> None:
    ledger = _Ledger(balance=0)
    backend = _Backend()

    result = await _tool(ledger, backend).execute(prompt="a lighthouse at dusk")

    assert result.is_error is True
    assert backend.calls == []
    assert result.metadata["reason"] == "insufficient_credits"


@pytest.mark.asyncio
@pytest.mark.usefixtures("owner_context")
async def test_the_hold_covers_every_image_asked_for() -> None:
    """One image is affordable; two are not."""
    ledger = _Ledger(balance=_ONE_IMAGE + 1)
    backend = _Backend()

    result = await _tool(ledger, backend).execute(prompt="a lighthouse at dusk", count=2)

    assert result.is_error is True
    assert backend.calls == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("owner_context")
async def test_past_the_daily_cap_the_provider_is_not_called() -> None:
    """M2: the cap used to be checked only at settle, where its refusal was swallowed."""
    ledger = _Ledger(day_cap=10, day_spent=10 - _ONE_IMAGE + 1)
    backend = _Backend()

    result = await _tool(ledger, backend).execute(prompt="a lighthouse at dusk")

    assert result.is_error is True
    assert backend.calls == []
    assert result.metadata["reason"] == "daily_spend_cap"
    assert "midnight UTC" in result.content
    assert ledger.rows == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("owner_context")
async def test_two_calls_cannot_spend_the_same_balance() -> None:
    """The hold is taken before the provider, so a concurrent second image is refused."""
    ledger = _Ledger(balance=_ONE_IMAGE + 1)
    hook = _hook(ledger)
    first = await hook.authorize(ImageGenOptions(), provider="openai", model="gpt-image-1")
    assert first is not None

    with pytest.raises(ImageChargeRefusedError):
        await hook.authorize(ImageGenOptions(), provider="openai", model="gpt-image-1")


@pytest.mark.asyncio
@pytest.mark.usefixtures("owner_context")
async def test_an_unreachable_ledger_refuses_before_the_provider() -> None:
    ledger = _Ledger()
    ledger.broken.add("deduct_idempotent")
    backend = _Backend()

    result = await _tool(ledger, backend).execute(prompt="a lighthouse at dusk")

    assert result.is_error is True
    assert backend.calls == []
    assert result.metadata["reason"] == "billing_unavailable"


# ---------------------------------------------------------------------------
# After the provider: the true-up, and its retries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("owner_context")
async def test_fewer_images_returned_than_asked_are_charged_as_returned() -> None:
    """Two held, one came back: the person pays for one."""
    ledger = _Ledger()

    result = await _tool(ledger, _Backend(returns=1)).execute(prompt="a lighthouse", count=2)

    assert result.is_error is False
    assert ledger.image_charged == _ONE_IMAGE
    (trueup,) = [r for r in ledger.image_rows() if r["method"] == "grant_idempotent"]
    assert trueup["amount"] == _TWO_IMAGES - _ONE_IMAGE
    assert trueup["cost_cents"] == pytest.approx(_ONE_IMAGE_CENTS)


@pytest.mark.asyncio
@pytest.mark.usefixtures("owner_context")
async def test_a_redelivered_settle_is_not_billed_twice() -> None:
    ledger = _Ledger()
    charge = await _hook(ledger).authorize(
        ImageGenOptions(count=1), provider="openai", model="gpt-image-1"
    )
    assert charge is not None
    result = await _Backend().generate("a lighthouse", options=ImageGenOptions(count=1))

    await charge.settle(result)
    await charge.settle(result)

    assert ledger.image_charged == _ONE_IMAGE
    assert ledger.attempts["capture_up_to_idempotent"] == 2, "the repeat reached the key gate"


@pytest.mark.asyncio
@pytest.mark.usefixtures("owner_context")
async def test_a_settle_retried_after_a_dropped_commit_is_billed_once() -> None:
    """The first write lands and the connection drops; the retry reuses the key."""
    ledger = _Ledger()
    ledger.drop_after_commit.add("capture_up_to_idempotent")

    result = await _tool(ledger).execute(prompt="a lighthouse at dusk")

    assert result.is_error is False
    assert ledger.attempts["capture_up_to_idempotent"] == 2, "a failed write is retried once"
    assert ledger.image_charged == _ONE_IMAGE


@pytest.mark.asyncio
@pytest.mark.usefixtures("owner_context")
async def test_a_hold_retried_after_a_dropped_commit_is_held_once() -> None:
    ledger = _Ledger()
    ledger.drop_after_commit.add("deduct_idempotent")

    result = await _tool(ledger).execute(prompt="a lighthouse at dusk")

    assert result.is_error is False
    assert ledger.attempts["deduct_idempotent"] == 2
    assert ledger.image_charged == _ONE_IMAGE


@pytest.mark.asyncio
@pytest.mark.usefixtures("owner_context")
async def test_two_calls_are_two_charges() -> None:
    """The key is per call: a second image is a second real cost, not a retry."""
    ledger = _Ledger()
    tool = _tool(ledger)

    await tool.execute(prompt="a lighthouse at dusk")
    await tool.execute(prompt="a lighthouse at dawn")

    assert ledger.image_charged == 2 * _ONE_IMAGE
    holds = [r for r in ledger.image_rows() if r["method"] == "deduct_idempotent"]
    assert len({r["billing_key"] for r in holds}) == 2


@pytest.mark.asyncio
@pytest.mark.usefixtures("owner_context")
async def test_a_settle_that_cannot_be_written_leaves_a_durable_record(
    audit_rows: list[dict[str, Any]],
) -> None:
    """L2: the image is kept, and the miss is an audit row, not only a log line."""
    ledger = _Ledger()
    ledger.broken.add("capture_up_to_idempotent")

    result = await _tool(ledger).execute(prompt="a lighthouse at dusk")

    assert result.is_error is False, "a ledger failure must not take the image away"
    (row,) = audit_rows
    assert row["action"] == "image_chat_unbilled"
    assert row["user_id"] == _OWNER
    assert row["target"].startswith(f"{CHAT_IMAGE_REASON}:")
    assert row["metadata"]["cost_cents"] == "4.2"
    assert "prompt" not in row["metadata"], "no content in the audit row"


@pytest.mark.asyncio
@pytest.mark.usefixtures("owner_context")
async def test_a_settle_write_failure_reaches_the_tool_as_a_ledger_error() -> None:
    ledger = _Ledger()
    ledger.broken.add("capture_up_to_idempotent")
    charge = await _hook(ledger).authorize(ImageGenOptions(), provider="openai", model="m")
    assert charge is not None
    result = await _Backend().generate("a lighthouse")

    with pytest.raises(ImageChargeLedgerError):
        await charge.settle(result)


@pytest.mark.asyncio
@pytest.mark.usefixtures("owner_context")
async def test_a_stop_pressed_during_the_hold_gives_the_hold_back(
    audit_rows: list[dict[str, Any]],
) -> None:
    """L1: the hold's thread commits after the turn is cancelled; nothing may stay held."""
    ledger = _Ledger()
    ledger.gated.add("deduct_idempotent")
    backend = _Backend()
    task = asyncio.create_task(_tool(ledger, backend).execute(prompt="a lighthouse at dusk"))

    assert await asyncio.to_thread(ledger.entered.wait, 10), "the hold never started"
    task.cancel()
    await asyncio.sleep(0)  # let the cancel land while the hold is still writing
    ledger.gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert backend.calls == [], "a cancelled call reached the provider"
    assert ledger.net_charged == 0, "the hold was stranded by the cancel"
    methods = [r["method"] for r in ledger.image_rows()]
    assert methods == ["deduct_idempotent", "grant_idempotent"]
    (release,) = [r for r in ledger.image_rows() if r["method"] == "grant_idempotent"]
    assert release["billing_key"].endswith(":release")
    assert audit_rows == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("owner_context")
async def test_a_double_clicked_stop_during_the_hold_still_gives_it_back(
    audit_rows: list[dict[str, Any]],
) -> None:
    """A second cancel while the undo waits must not be forwarded into the hold."""
    ledger = _Ledger()
    ledger.gated.add("deduct_idempotent")
    backend = _Backend()
    task = asyncio.create_task(_tool(ledger, backend).execute(prompt="a lighthouse at dusk"))

    assert await asyncio.to_thread(ledger.entered.wait, 10), "the hold never started"
    task.cancel()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()  # ChatTurnRegistry.request_cancel again, while the task is not done
    await asyncio.sleep(0)
    ledger.gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert backend.calls == []
    assert ledger.net_charged == 0, "the second cancel stranded the hold"
    assert [r["method"] for r in ledger.image_rows()] == ["deduct_idempotent", "grant_idempotent"]
    assert audit_rows == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("owner_context")
async def test_a_bug_in_the_cancel_undo_does_not_replace_the_cancel(
    monkeypatch: pytest.MonkeyPatch, audit_rows: list[dict[str, Any]]
) -> None:
    """A non-ledger error from release during the undo is recorded; the cancel still wins."""
    ledger = _Ledger()
    ledger.gated.add("deduct_idempotent")

    async def _broken_release(_self: Any) -> None:
        msg = "release bug"
        raise ZeroDivisionError(msg)

    monkeypatch.setattr(chat_charge_module.ChatImageCharge, "release", _broken_release)
    task = asyncio.create_task(_tool(ledger).execute(prompt="a lighthouse at dusk"))

    assert await asyncio.to_thread(ledger.entered.wait, 10)
    task.cancel()
    await asyncio.sleep(0)
    ledger.gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    (row,) = audit_rows
    assert row["action"] == "image_chat_unrefunded"
    assert row["metadata"]["cause"] == "error:ZeroDivisionError"


@pytest.mark.asyncio
@pytest.mark.usefixtures("owner_context")
async def test_a_stop_pressed_during_a_refused_hold_releases_nothing() -> None:
    ledger = _Ledger(balance=0)
    ledger.gated.add("deduct_idempotent")
    task = asyncio.create_task(_tool(ledger).execute(prompt="a lighthouse at dusk"))

    assert await asyncio.to_thread(ledger.entered.wait, 10)
    task.cancel()
    await asyncio.sleep(0)
    ledger.gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert ledger.rows == [], "nothing was held, so nothing may be given back"


@pytest.mark.asyncio
@pytest.mark.usefixtures("owner_context")
async def test_an_uncertain_hold_is_recorded_not_released(
    audit_rows: list[dict[str, Any]],
) -> None:
    """L2: the hold may have landed before the connection died; reconciliation must find it."""
    ledger = _Ledger()
    ledger.drop_then_break.add("deduct_idempotent")
    backend = _Backend()

    result = await _tool(ledger, backend).execute(prompt="a lighthouse at dusk")

    assert result.is_error is True
    assert result.metadata["reason"] == "billing_unavailable"
    assert backend.calls == []
    assert [r["method"] for r in ledger.rows] == ["deduct_idempotent"], "no blind release"
    (row,) = audit_rows
    assert row["action"] == "image_chat_hold_uncertain"
    assert row["target"].startswith(f"{CHAT_IMAGE_REASON}:")
    assert row["metadata"]["held"] == str(_ONE_IMAGE)
    assert row["metadata"]["billing_key"] == row["target"]
    assert "prompt" not in row["metadata"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("owner_context")
async def test_a_bug_in_settle_keeps_the_image_and_leaves_a_record(
    monkeypatch: pytest.MonkeyPatch, audit_rows: list[dict[str, Any]]
) -> None:
    """L3: the person paid the hold; a bug must never also take the image away."""
    ledger = _Ledger()

    def _broken_pricing(*_: Any, **__: Any) -> Any:
        msg = "pricing bug"
        raise ZeroDivisionError(msg)

    monkeypatch.setattr(chat_charge_module, "generation_cost_cents", _broken_pricing)

    result = await _tool(ledger).execute(prompt="a lighthouse at dusk")

    assert result.is_error is False, "a bug in settle took the image away"
    (row,) = audit_rows
    assert row["action"] == "image_chat_unbilled"
    assert row["metadata"]["cause"] == "error:ZeroDivisionError"


# ---------------------------------------------------------------------------
# Failures charge nothing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("owner_context")
async def test_a_provider_failure_charges_nothing() -> None:
    ledger = _Ledger()
    backend = _Backend(side_effect=ImageProviderError("upstream 503", context={"reason": "x"}))

    result = await _tool(ledger, backend).execute(prompt="a lighthouse at dusk")

    assert result.is_error is True
    assert len(backend.calls) == 1
    assert ledger.net_charged == 0, "the hold was not given back"
    methods = [r["method"] for r in ledger.image_rows()]
    assert methods == ["deduct_idempotent", "grant_idempotent"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("owner_context")
async def test_a_redelivered_release_refunds_once() -> None:
    ledger = _Ledger()
    charge = await _hook(ledger).authorize(ImageGenOptions(), provider="openai", model="m")
    assert charge is not None

    await charge.release()
    await charge.release()

    assert ledger.net_charged == 0


# ---------------------------------------------------------------------------
# Nothing to bill
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("owner_context")
async def test_the_community_edition_makes_images_and_charges_nothing() -> None:
    backend = _Backend()

    result = await _tool(UnlimitedCreditsPolicy(), backend).execute(prompt="a lighthouse")

    assert result.is_error is False
    assert len(backend.calls) == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("owner_context")
async def test_no_billing_configured_makes_images_and_charges_nothing() -> None:
    backend = _Backend()

    result = await _tool(None, backend).execute(prompt="a lighthouse")

    assert result.is_error is False
    assert len(backend.calls) == 1


@pytest.mark.asyncio
async def test_no_request_owner_charges_nothing() -> None:
    """The CLI, and a task leg, bind no owner (leg images are R9-309)."""
    ledger = _Ledger()
    backend = _Backend()

    result = await _tool(ledger, backend).execute(prompt="a lighthouse")

    assert result.is_error is False
    assert len(backend.calls) == 1
    assert ledger.rows == []


@pytest.mark.asyncio
async def test_a_surface_that_cannot_deliver_images_is_not_charged() -> None:
    ledger = _Ledger()
    token = _bind(delivers_images=False)
    try:
        charge = await _hook(ledger).authorize(ImageGenOptions(), provider="openai", model="m")
    finally:
        reset_sandbox_request_context(token)

    assert charge is None
    assert ledger.rows == []


# ---------------------------------------------------------------------------
# The composition root wires it
# ---------------------------------------------------------------------------


def _factory(
    ledger: _Ledger, backend: _Backend, tmp_path: Path, *, api_config: APIConfig | None = None
) -> RuntimeFactory:
    return RuntimeFactory(
        rls_engine=object(),  # type: ignore[arg-type]
        embedder=None,  # type: ignore[arg-type]
        tier_registry=None,  # type: ignore[arg-type]
        turn_log_writer=None,  # type: ignore[arg-type]
        audit_root=tmp_path / "audit",
        sandbox_pool=None,
        workspace_root=None,
        image_backend=backend,
        credits_policy=ledger,  # type: ignore[arg-type]
        api_config=api_config,
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("owner_context")
async def test_the_runtime_factory_charges_the_chat_tool(tmp_path: Path) -> None:
    ledger = _Ledger()
    persona = Persona(
        persona_id=_PERSONA,
        identity=PersonaIdentity(name="Astrid", role="assistant", background="Draws things."),
        tools=["generate_image"],
    )
    factory = _factory(ledger, _Backend(), tmp_path)
    toolbox: Any = await factory._build_toolbox(persona, scanned_skills=[])

    result = await toolbox.dispatch(
        ToolCall(name="generate_image", args={"prompt": "a lighthouse"}, call_id="call_1")
    )

    assert result.is_error is False, result.content
    assert ledger.image_charged == _ONE_IMAGE
    assert {r["user_id"] for r in ledger.image_rows()} == {_OWNER}


@pytest.mark.asyncio
@pytest.mark.usefixtures("owner_context")
async def test_the_factory_charges_at_the_configured_image_floor(tmp_path: Path) -> None:
    """L5: ``image_credit_floor`` reaches the chat hook, so a cheap image costs the floor."""
    floor = _ONE_IMAGE + 3
    config = APIConfig(
        edition=Edition.community,
        community_db_path=tmp_path / "community.db",
        community_memory_path=tmp_path / "memory",
        audit_root=str(tmp_path / "audit"),
        workspace_root=tmp_path / "workspace",
        image_credit_floor=floor,
    )
    ledger = _Ledger()
    hook: Any = _factory(ledger, _Backend(), tmp_path, api_config=config)._chat_image_charge_hook()

    charge = await hook.authorize(ImageGenOptions(), provider="openai", model="gpt-image-1")
    await charge.settle(await _Backend().generate("a lighthouse"))

    assert ledger.image_charged == floor
