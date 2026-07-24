"""Plan catalog + ``$``↔credit presentation (Spec M4, T1c; D-M4-5 / D-M4-R1).

The Free / Plus / Pro plan catalog (code/config, owner-locked Phase-4 numbers) + the
PAYG dollar-pack catalog + the dollar-wallet presentation helper. This is pure,
edition-agnostic DATA: it holds the plan → model-set *mapping* and the plan economics,
NOT the actual model strings (those stay in ``PERSONA_<TIER>_MODELS`` env, owner-set at
go-live) and NOT any enforcement (the per-plan model policy is T5; the paywall/webhook
is T2/T3). Under the community edition the catalog is inert — there is no paywall.

Accounting: ``1 credit = 1¢`` (M3). Plan prices and allowances are stored in **credits**
(= cents) so they compose with the ledger without conversion; the ``$`` view is the
:func:`credits_to_dollars` / :func:`format_dollars` presentation layer (D-M4-1).
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "DEFAULT_PLAN_CODE",
    "PAYG_PACKS",
    "PLANS",
    "PaygPack",
    "Plan",
    "PlanCode",
    "PlanModelSet",
    "all_plans",
    "credits_to_dollars",
    "default_plan",
    "dollars_to_credits",
    "format_dollars",
    "get_payg_pack",
    "get_plan",
]

#: Cents per dollar — the ledger unit is 1 credit = 1¢ (M3), so 1 dollar = 100 credits.
_CENTS_PER_DOLLAR = 100


class PlanCode(StrEnum):
    """The plan ladder (D-M4-5). ``free`` is the permanent default (D-M4-9)."""

    free = "free"
    plus = "plus"
    pro = "pro"


class PlanModelSet(BaseModel):
    """A plan's allowed-model-set MAPPING (T5 resolves the env strings at runtime).

    The catalog never hardcodes model strings — it references the ``PERSONA_<TIER>_MODELS``
    env var NAMES the owner populates at go-live (position 0 = primary). The load-bearing
    cost-safety flag is :attr:`allows_paid_fallback`: the Free plan resolves a FREE-ONLY
    chain with NO paid fallback (a free user can never reach a paid model — T5 enforces this
    at backend construction); paid plans keep the Claude fallback.

    Attributes:
        frontier_models_env: Env var name holding the plan's frontier (chat) model list.
        mid_models_env: Env var name holding the plan's mid (voice / background) model list.
        allows_paid_fallback: Whether the fallback walk may reach a paid model. ``False``
            for Free (the no-paid-fallback hard rule, D-M4-4); ``True`` for paid plans.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    frontier_models_env: str
    mid_models_env: str
    allows_paid_fallback: bool


class Plan(BaseModel):
    """One subscription plan (frozen; the catalog is immutable config).

    Prices/allowances are in **credits** (1 credit = 1¢); the ``$`` view is the
    presentation helper. Owner-locked Phase-4 numbers (D-M4-R1).

    Attributes:
        code: The plan identity.
        monthly_price_credits: The subscription price in credits (``0`` for Free).
        included_allowance_credits: The monthly allowance granted/reset (no rollover).
        model_set: The plan → allowed-model-set mapping + the no-paid-fallback flag.
        payg_eligible: Whether this plan may buy PAYG dollar packs (top-up at 0).
        auto_topup_eligible: Whether opt-in auto-top-up is offered (Pro only, D-M4-6).
        is_default: Whether this is the permanent default plan a user starts on (Free).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: PlanCode
    monthly_price_credits: int = Field(ge=0)
    included_allowance_credits: int = Field(ge=0)
    model_set: PlanModelSet
    payg_eligible: bool
    auto_topup_eligible: bool
    is_default: bool

    @property
    def monthly_price_dollars(self) -> Decimal:
        """The subscription price as a ``$`` :class:`~decimal.Decimal`."""
        return credits_to_dollars(self.monthly_price_credits)

    @property
    def included_allowance_dollars(self) -> Decimal:
        """The monthly allowance as a ``$`` :class:`~decimal.Decimal`."""
        return credits_to_dollars(self.included_allowance_credits)

    @property
    def low_balance_threshold_credits(self) -> int:
        """The per-plan low-balance warning line: 20% of the included allowance (Spec M4, T8).

        Replaces the retired flat ``LOW_BALANCE_THRESHOLD`` (10_000 — nonsensical against a
        $3 free allowance, which would read "always low"). Free ⇒ 60 ($0.60), Plus ⇒ 400
        ($4), Pro ⇒ 1200 ($12). A PAYG-only / absent-subscription user warns on the Free
        line (the caller resolves absent → free). Floored at 1 so a plan can never warn at
        exactly 0 (0 is exhaustion, not "low").
        """
        return max(1, self.included_allowance_credits // 5)


class PaygPack(BaseModel):
    """A one-time PAYG dollar pack (frozen). Purchase is 1:1 — ``$X`` buys ``X`` credits
    (``X*100``); the markup is applied on SPEND (M3), not purchase. Per-lot 12-month
    expiry (D-M4-5); the granting + expiry live on ``payg_grants`` (T1a/T4).

    Attributes:
        price_credits: The pack price in credits (= the credits granted, 1:1).
        expiry_months: Months until a purchased lot expires (12).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    price_credits: int = Field(gt=0)
    expiry_months: int = Field(default=12, gt=0)

    @property
    def granted_credits(self) -> int:
        """Credits granted by this pack (1:1 with the price)."""
        return self.price_credits

    @property
    def price_dollars(self) -> Decimal:
        """The pack price as a ``$`` :class:`~decimal.Decimal`."""
        return credits_to_dollars(self.price_credits)


# --- The ``$``↔credit presentation helper (D-M4-1) ---------------------------


def dollars_to_credits(dollars: Decimal | int | str) -> int:
    """``$X`` → ``X * 100`` credits (the grant side). Exact via :class:`~decimal.Decimal`.

    Accepts a :class:`~decimal.Decimal`, an ``int`` (whole dollars), or a ``str``
    (``"5.00"``) — never a binary ``float`` (money must not carry float noise). Rounds
    half-up to the nearest whole credit (a sub-cent price is a config error, but the
    round keeps the result an integer credit count).
    """
    cents = Decimal(str(dollars)) * _CENTS_PER_DOLLAR
    return int(cents.to_integral_value(rounding=ROUND_HALF_UP))


def credits_to_dollars(credit_amount: int) -> Decimal:
    """``credit_amount`` → ``$`` (the display side): ``credits / 100`` as an exact Decimal.

    ``500`` → ``Decimal('5')``; ``550`` → ``Decimal('5.5')``. Round-trips with
    :func:`dollars_to_credits` (``$5.00 ↔ 500``).
    """
    return Decimal(credit_amount) / _CENTS_PER_DOLLAR


def format_dollars(credit_amount: int) -> str:
    """``credit_amount`` → a ``"$X.XX"`` string (2-decimal, half-up). ``550`` → ``"$5.50"``."""
    amount = credits_to_dollars(credit_amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"${amount}"


# --- The catalog (owner-locked Phase-4 numbers, D-M4-R1) ---------------------

# Free: FREE-ONLY models, NO paid fallback (the load-bearing cost-safety, D-M4-4).
_FREE_MODEL_SET = PlanModelSet(
    frontier_models_env="PERSONA_FREE_FRONTIER_MODELS",
    mid_models_env="PERSONA_FREE_MID_MODELS",
    allows_paid_fallback=False,
)
# Paid: the existing prod tiers (kimi-k3→sonnet frontier; kimi-k2-0905→gpt-oss-120b→sonnet
# mid), Claude fallback allowed. The env strings are the owner's go-live config.
_PAID_MODEL_SET = PlanModelSet(
    frontier_models_env="PERSONA_FRONTIER_MODELS",
    mid_models_env="PERSONA_MID_MODELS",
    allows_paid_fallback=True,
)

_PLANS: dict[PlanCode, Plan] = {
    PlanCode.free: Plan(
        code=PlanCode.free,
        monthly_price_credits=0,  # $0
        included_allowance_credits=300,  # $3 allowance (= the per-free-user subsidy ceiling)
        model_set=_FREE_MODEL_SET,
        payg_eligible=True,  # a free user at 0 may top up instead of subscribing (funnel)
        auto_topup_eligible=False,
        is_default=True,  # the permanent default (D-M4-9)
    ),
    PlanCode.plus: Plan(
        code=PlanCode.plus,
        monthly_price_credits=1500,  # $15/mo
        included_allowance_credits=2000,  # ~$20 included
        model_set=_PAID_MODEL_SET,
        payg_eligible=True,
        auto_topup_eligible=False,
        is_default=False,
    ),
    PlanCode.pro: Plan(
        code=PlanCode.pro,
        monthly_price_credits=5000,  # $50/mo
        included_allowance_credits=6000,  # ~$60 included
        model_set=_PAID_MODEL_SET,
        payg_eligible=True,
        auto_topup_eligible=True,  # opt-in auto-top-up (D-M4-6)
        is_default=False,
    ),
}

#: The immutable plan catalog (read-only view over the private registry).
PLANS: MappingProxyType[PlanCode, Plan] = MappingProxyType(_PLANS)

#: The PAYG dollar packs (D-M4-5): $5 / $10 / $25 / $50, 12-month per-lot expiry.
PAYG_PACKS: tuple[PaygPack, ...] = (
    PaygPack(price_credits=500),
    PaygPack(price_credits=1000),
    PaygPack(price_credits=2500),
    PaygPack(price_credits=5000),
)

#: PAYG packs keyed by their dollar amount as a string (``'5'`` → the $5 pack).
_PAYG_PACKS_BY_KEY: dict[str, PaygPack] = {
    str(pack.price_credits // _CENTS_PER_DOLLAR): pack for pack in PAYG_PACKS
}

#: The plan a user starts on / falls back to — permanent Free (D-M4-9).
DEFAULT_PLAN_CODE: PlanCode = PlanCode.free


def get_plan(code: PlanCode | str) -> Plan:
    """The plan for ``code`` (accepts the enum or its string value).

    Raises:
        KeyError: an unknown plan code.
    """
    return PLANS[PlanCode(code)]


def all_plans() -> tuple[Plan, ...]:
    """The catalog in ladder order (Free, Plus, Pro)."""
    return (PLANS[PlanCode.free], PLANS[PlanCode.plus], PLANS[PlanCode.pro])


def default_plan() -> Plan:
    """The permanent default plan (Free)."""
    return PLANS[DEFAULT_PLAN_CODE]


def get_payg_pack(key: str) -> PaygPack | None:
    """The PAYG pack for a dollar key (``'5'`` / ``'10'`` / ``'25'`` / ``'50'``), else ``None``."""
    return _PAYG_PACKS_BY_KEY.get(key)
