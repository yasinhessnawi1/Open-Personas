"""R9-308: an image a persona makes in chat is charged to the owner, once, at what it cost.

The chat ``generate_image`` tool used to call the backend directly, so every image made in
a chat turn cost the house the provider price and the person nothing. The api now injects
:class:`ChatImageChargeHook` into the tool: it checks the balance before the provider is
called, and after a success it charges what the images cost, priced through the same
``generation_cost_cents`` as the request path, as one idempotent ledger entry per call.

Everything here runs against a recording ledger and an in-memory backend: no database, no
provider, no network, no Stripe.
"""

# ruff: noqa: ANN401, ARG002 (the fakes mirror real keyword signatures)
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from persona.backends.types import StreamChunk
from persona.billing import BillingConfig, credits_charged
from persona.errors import CreditsExhaustedError
from persona.imagegen import (
    GeneratedImage,
    GenerationResult,
    ImageBackend,
    ImageGenOptions,
    ImageProviderError,
    make_generate_image_tool,
)
from persona.schema.conversation import Conversation
from persona.schema.persona import Persona, PersonaIdentity
from persona.schema.tools import ToolCall
from persona_api.background.chat_turn_worker import ChatTurnRegistry
from persona_api.editions import UnlimitedCreditsPolicy
from persona_api.imagegen.chat_charge import ChatImageChargeHook
from persona_api.sandbox import (
    SandboxRequestContext,
    reset_sandbox_request_context,
    set_sandbox_request_context,
)
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


class _Backend:
    """OpenAI-shaped: reports no cost, so the registry's per-image price applies."""

    provider_name = "openai"
    model_name = "gpt-image-1"

    def __init__(self, *, side_effect: BaseException | None = None) -> None:
        self._side_effect = side_effect
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


class _Ledger:
    """A credits policy double with a balance, honouring billing keys like the real table.

    ``capture_up_to_idempotent`` mirrors ``ON CONFLICT (billing_key) DO NOTHING``: a key
    seen before charges nothing. ``fail_after_commit`` makes the next keyed capture land
    and THEN raise, the shape of a connection dropped after the commit.
    """

    def __init__(self, *, balance: int = 1_000) -> None:
        self.balance = balance
        self.charges: list[dict[str, Any]] = []
        self.keys: set[str] = set()
        self.capture_attempts = 0
        self.fail_after_commit = False

    @property
    def charged(self) -> int:
        return sum(int(c["amount"]) for c in self.charges)

    def require_credits(self, *, rls_engine: Any, user_id: str) -> int:
        if self.balance <= 0:
            raise CreditsExhaustedError(context={"balance": str(self.balance)})
        return self.balance

    def _take(self, kind: str, kw: dict[str, Any]) -> int:
        taken = min(int(kw["amount"]), self.balance)
        self.balance -= taken
        self.charges.append({**kw, "kind": kind, "amount": taken})
        return taken

    def deduct(self, **kw: Any) -> int:
        self._take("deduct", kw)
        return self.balance

    def capture_up_to(self, **kw: Any) -> tuple[int, int]:
        return self._take("capture_up_to", kw), self.balance

    def capture_up_to_idempotent(self, **kw: Any) -> tuple[int, int]:
        self.capture_attempts += 1
        key = kw["billing_key"]
        if key in self.keys:
            return 0, self.balance
        self.keys.add(key)
        taken = self._take("capture_up_to_idempotent", kw)
        if self.fail_after_commit:
            self.fail_after_commit = False
            msg = "connection dropped after commit"
            raise ConnectionError(msg)
        return taken, self.balance


def _hook(ledger: Any) -> ChatImageChargeHook:
    return ChatImageChargeHook(
        credits_policy=ledger,
        rls_engine=object(),  # type: ignore[arg-type]  # the double ignores it
        cost_source=None,
        billing_config=BillingConfig(),
        floor=1,
    )


def _tool(ledger: Any, backend: _Backend | None = None) -> AsyncTool:
    return make_generate_image_tool(
        backend=backend or _Backend(), persona_id=_PERSONA, charge_hook=_hook(ledger)
    )


@pytest.fixture
def owner_context() -> Iterator[None]:
    """The chat worker binds this around every turn; the hook reads the owner from it."""
    token = set_sandbox_request_context(
        SandboxRequestContext(owner_id=_OWNER, conversation_id=_CONV)
    )
    try:
        yield
    finally:
        reset_sandbox_request_context(token)


def _image_charges(ledger: _Ledger) -> list[dict[str, Any]]:
    return [c for c in ledger.charges if str(c["reason"]).startswith("chat_image")]


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


async def _run_turn(ledger: _Ledger, *, with_image: bool, count: int = 1) -> None:
    registry = ChatTurnRegistry(
        sink=_Sink(),
        rls_engine=object(),  # type: ignore[arg-type]
        credits_policy=ledger,  # type: ignore[arg-type]
    )
    loop = _ImageTurnLoop(_tool(ledger) if with_image else None, count=count)
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

    one_image = credits_charged(
        provider_cents=_ONE_IMAGE_CENTS, infra_flat_cents=0.0, markup=1.0, floor=1
    )
    assert with_image.charged - without.charged == one_image
    (image,) = _image_charges(with_image)
    assert image["user_id"] == _OWNER, "the image is charged to the turn's owner"
    assert image["cost_cents"] == pytest.approx(_ONE_IMAGE_CENTS)
    assert image["cost_basis"] != "unpriced"
    assert _image_charges(without) == []


@pytest.mark.asyncio
async def test_two_images_in_a_turn_cost_twice_one() -> None:
    one = _Ledger()
    two = _Ledger()

    await _run_turn(one, with_image=True, count=1)
    await _run_turn(two, with_image=True, count=2)

    (one_charge,) = _image_charges(one)
    (two_charge,) = _image_charges(two)
    assert two_charge["cost_cents"] == pytest.approx(2 * one_charge["cost_cents"])
    assert two_charge["amount"] == credits_charged(
        provider_cents=2 * _ONE_IMAGE_CENTS, infra_flat_cents=0.0, markup=1.0, floor=1
    )


# ---------------------------------------------------------------------------
# The hook's own rules
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("owner_context")
async def test_an_unaffordable_image_does_not_call_the_provider() -> None:
    ledger = _Ledger(balance=3)  # one image costs 5 credits
    backend = _Backend()

    result = await _tool(ledger, backend).execute(prompt="a lighthouse at dusk")

    assert result.is_error is True
    assert backend.calls == [], "the provider was called for an image the owner cannot pay for"
    assert result.metadata["reason"] == "insufficient_credits"
    assert "credits" in result.content
    assert ledger.charges == []


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
async def test_the_balance_check_covers_every_image_asked_for() -> None:
    """One image (5 credits) is affordable at 6; two (9 credits) are not."""
    ledger = _Ledger(balance=6)
    backend = _Backend()

    result = await _tool(ledger, backend).execute(prompt="a lighthouse at dusk", count=2)

    assert result.is_error is True
    assert backend.calls == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("owner_context")
async def test_a_redelivered_settle_is_not_billed_twice() -> None:
    ledger = _Ledger()
    hook = _hook(ledger)
    charge = await hook.authorize(ImageGenOptions(count=1), provider="openai", model="gpt-image-1")
    assert charge is not None
    result = await _Backend().generate("a lighthouse", options=ImageGenOptions(count=1))

    await charge.settle(result)
    await charge.settle(result)

    assert len(ledger.charges) == 1
    assert ledger.capture_attempts == 2, "the second settle must reach the ledger's key gate"


@pytest.mark.asyncio
@pytest.mark.usefixtures("owner_context")
async def test_a_settle_retried_after_a_dropped_commit_is_billed_once() -> None:
    """The first write lands and the connection drops; the retry reuses the key."""
    ledger = _Ledger()
    ledger.fail_after_commit = True

    result = await _tool(ledger).execute(prompt="a lighthouse at dusk")

    assert result.is_error is False
    assert ledger.capture_attempts == 2, "a failed write is retried once"
    assert len(_image_charges(ledger)) == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("owner_context")
async def test_two_calls_are_two_charges() -> None:
    """The key is per call: a second image is a second real cost, not a retry."""
    ledger = _Ledger()
    tool = _tool(ledger)

    await tool.execute(prompt="a lighthouse at dusk")
    await tool.execute(prompt="a lighthouse at dawn")

    charges = _image_charges(ledger)
    assert len(charges) == 2
    assert charges[0]["billing_key"] != charges[1]["billing_key"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("owner_context")
async def test_a_provider_failure_charges_nothing() -> None:
    ledger = _Ledger()
    backend = _Backend(side_effect=ImageProviderError("upstream 503", context={"reason": "x"}))

    result = await _tool(ledger, backend).execute(prompt="a lighthouse at dusk")

    assert result.is_error is True
    assert len(backend.calls) == 1
    assert ledger.charges == []
    assert ledger.capture_attempts == 0


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
    """The CLI and other callers with no bound owner keep today's behaviour."""
    ledger = _Ledger()
    backend = _Backend()

    result = await _tool(ledger, backend).execute(prompt="a lighthouse")

    assert result.is_error is False
    assert len(backend.calls) == 1
    assert ledger.charges == []


# ---------------------------------------------------------------------------
# The composition root wires it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("owner_context")
async def test_the_runtime_factory_charges_the_chat_tool(tmp_path: Path) -> None:
    ledger = _Ledger()
    backend = _Backend()
    factory = RuntimeFactory(
        rls_engine=object(),  # type: ignore[arg-type]
        embedder=None,  # type: ignore[arg-type]
        tier_registry=None,  # type: ignore[arg-type]
        turn_log_writer=None,  # type: ignore[arg-type]
        audit_root=tmp_path / "audit",
        sandbox_pool=None,
        workspace_root=None,
        image_backend=backend,
        credits_policy=ledger,  # type: ignore[arg-type]
    )
    persona = Persona(
        persona_id=_PERSONA,
        identity=PersonaIdentity(name="Astrid", role="assistant", background="Draws things."),
        tools=["generate_image"],
    )
    toolbox: Any = await factory._build_toolbox(persona, scanned_skills=[])

    result = await toolbox.dispatch(
        ToolCall(name="generate_image", args={"prompt": "a lighthouse"}, call_id="call_1")
    )

    assert result.is_error is False, result.content
    (charge,) = _image_charges(ledger)
    assert charge["user_id"] == _OWNER
    assert charge["cost_cents"] == pytest.approx(_ONE_IMAGE_CENTS)
