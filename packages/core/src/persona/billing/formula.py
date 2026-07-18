"""The one credit formula + its env-driven config (Spec M3, D-M3-2/3/4).

One formula, applied by :class:`~persona.billing.metered.MeteredBilling` at every
paid surface:

    credits_charged = max(floor, ceil( MARKUP × (provider_cents + infra_flat_cents) ))

at ``1 credit = 1¢``. ``MARKUP`` (``PERSONA_CREDIT_MARKUP``) defaults to ``1.0``
(pure pass-through — matches today's economics; the ~1.4× margin is M4's to set).
The infra flat rates (``PERSONA_INFRA_RATE_*``) are the per-unit charge for our
own hosting/compute — the *sole* charge for zero-provider surfaces and an
*additive* component on provider surfaces (D-M3-4/6).

The ceil uses M2's ``Decimal(str(cost))`` PRINTED-value discipline (the same one
``chat_turn_worker._turn_charge`` uses): a genuinely-fractional cost rounds up
per ceil semantics while binary-float representation noise (e.g.
``9.000000000000002``) can never manufacture an extra credit.

Config is a frozen Pydantic-v2 :class:`BillingConfig` read from the environment
and INJECTED (no module-level global reads) per the engineering standards.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, Decimal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["BillingConfig", "credits_charged"]


class BillingConfig(BaseSettings):
    """Credit-billing tunables, read from ``PERSONA_*`` env vars (D-M3-3/R2).

    Frozen + injected. ``credit_markup`` is the global pass-through/margin
    multiplier (default ``1.0``); the ``infra_rate_*`` fields are the per-unit
    infra flat rates in cents (provisional defaults per D-M3-R2 — the owner may
    tune before ship; they are reversible env config, not code).

    Attributes:
        credit_markup: ``PERSONA_CREDIT_MARKUP`` — global multiplier on
            ``(provider_cents + infra_flat)``. ``1.0`` = pure pass-through.
        infra_rate_per_llm_call_cents: ``PERSONA_INFRA_RATE_PER_LLM_CALL_CENTS``
            — additive infra per model call (chat / authoring / agentic /
            background).
        infra_rate_per_image_cents: ``PERSONA_INFRA_RATE_PER_IMAGE_CENTS`` —
            additive infra per generated image.
        infra_rate_per_voice_min_cents:
            ``PERSONA_INFRA_RATE_PER_VOICE_MIN_CENTS`` — infra per voice minute
            (LiveKit transport + voice compute); the sole charge for transport.
        infra_rate_per_embed_batch_cents:
            ``PERSONA_INFRA_RATE_PER_EMBED_BATCH_CENTS`` — infra per standalone
            embedding batch (usually accrued into the enclosing op, Ruling B).
        infra_rate_per_tool_call_cents:
            ``PERSONA_INFRA_RATE_PER_TOOL_CALL_CENTS`` — infra per standalone
            connector / MCP call (usually accrued into the enclosing op).
        infra_rate_per_sandbox_exec_cents:
            ``PERSONA_INFRA_RATE_PER_SANDBOX_EXEC_CENTS`` — infra per sandbox
            execution (reclassifies the pre-M3 flat-1, standalone).
    """

    model_config = SettingsConfigDict(env_prefix="PERSONA_", extra="ignore", frozen=True)

    credit_markup: float = Field(default=1.0, ge=0.0)
    infra_rate_per_llm_call_cents: float = Field(default=0.1, ge=0.0)
    infra_rate_per_image_cents: float = Field(default=1.0, ge=0.0)
    infra_rate_per_voice_min_cents: float = Field(default=1.0, ge=0.0)
    infra_rate_per_embed_batch_cents: float = Field(default=0.1, ge=0.0)
    infra_rate_per_tool_call_cents: float = Field(default=0.1, ge=0.0)
    infra_rate_per_sandbox_exec_cents: float = Field(default=1.0, ge=0.0)


def credits_charged(
    *,
    provider_cents: float,
    infra_flat_cents: float,
    markup: float,
    floor: int,
) -> int:
    """The one credit formula: ``max(floor, ceil(markup × (provider + infra)))``.

    Args:
        provider_cents: The call's real provider cost in cents (an OpenRouter
            actual, a priced estimate, a summed provider meter, or ``0.0`` for a
            zero-provider surface).
        infra_flat_cents: The additive infra flat rate in cents for this unit
            (``0.0`` when none applies).
        markup: The :attr:`BillingConfig.credit_markup` multiplier (``1.0`` =
            pass-through).
        floor: The surface's minimum charge in whole credits (``>= 0``); a
            priceless/near-zero op never costs less than this.

    Returns:
        The whole-credit charge (``1 credit = 1¢``). The ceil is computed on the
        PRINTED value (``Decimal(str(...))``) so float representation noise never
        manufactures an extra credit (the M2 discipline). Negative inputs are
        clamped to ``0`` before the ceil (defensive — a bad upstream value must
        never REDUCE the floor).
    """
    total = markup * (max(0.0, provider_cents) + max(0.0, infra_flat_cents))
    ceiled = int(Decimal(str(total)).to_integral_value(rounding=ROUND_CEILING))
    return max(floor, ceiled)
