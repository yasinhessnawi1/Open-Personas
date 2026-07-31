"""Shared composition of the billed :class:`ChatTurnRegistry` (R9-079).

A chat turn costs the same money wherever it is typed. The hosted API composed
its registry in the app lifespan with the full billing set; the standalone
connector service composed its own with ``ChatTurnRegistry(sink=…, rls_engine=…)``
and NOTHING else — and every billing input on that constructor is keyword-only
WITH a default, so the omission was silent: ``credits_policy=None`` means "no
billing", and every Telegram / Discord / Slack / WhatsApp / SMS / email turn ran
for free.

Both composition roots now go through :func:`build_chat_turn_registry`, so the
billing inputs are derived from the SAME :class:`~persona_api.config.APIConfig`
fields by the SAME code on both surfaces. Adding a billing knob in one place can
no longer leave the other surface on the constructor defaults.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.billing import BillingConfig

from persona_api.background.chat_turn_worker import ChatTurnRegistry

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from persona_api.background.chat_turn_worker import ChatTurnSink
    from persona_api.billing import StripeGateway
    from persona_api.config import APIConfig
    from persona_api.editions.credits_policy import CreditsPolicy
    from persona_api.initiative.verb_service import InitiativeVerbService
    from persona_api.jobs.queue import JobQueue
    from persona_api.services.origination_service import OriginationService
    from persona_api.services.task_reschedule_service import TaskRescheduleService
    from persona_api.services.task_steering_service import TaskSteeringService

__all__ = ["build_chat_turn_registry"]


def build_chat_turn_registry(
    *,
    sink: ChatTurnSink,
    rls_engine: Engine,
    config: APIConfig,
    credits_policy: CreditsPolicy,
    gateway: StripeGateway | None,
    job_queue: JobQueue | None,
    origination_service: OriginationService | None = None,
    task_steering_service: TaskSteeringService | None = None,
    task_reschedule_service: TaskRescheduleService | None = None,
    initiative_verb_service: InitiativeVerbService | None = None,
) -> ChatTurnRegistry:
    """Build the detached chat-turn registry with the edition's full billing set.

    The billing half is NOT optional here — ``credits_policy`` is a required
    argument precisely because the underlying constructor defaults it to ``None``
    (= bill nothing), which is how the connector surface ran unbilled. A caller
    that has no metering passes the edition's
    :class:`~persona_api.editions.credits_policy.UnlimitedCreditsPolicy`
    explicitly (that is what ``build_credits_policy`` returns for community), so
    "unbilled" is always a stated decision rather than a forgotten argument.

    Args:
        sink: The turn persistence sink (``MessagesTurnSink``).
        rls_engine: The RLS-scoped engine the worker binds for its writes.
        config: The resolved API config — the single source of the per-turn
            credit floor, the proportional-billing switch, and the charge ceiling.
        credits_policy: The edition's credits policy (``build_credits_policy``).
        gateway: The Stripe gateway for Pro auto-top-up (``build_stripe_gateway``);
            ``None`` when billing is not active.
        job_queue: The durable queue for the off-critical-path synthesis enqueue
            at the turn boundary (Spec K2 T8d); ``None`` makes it a no-op.
        origination_service: Worker side of the A4 task-contract flow.
        task_steering_service: Worker side of the A4 steering verbs.
        task_reschedule_service: Worker side of the A8 reschedule verb.
        initiative_verb_service: Worker side of the A5 initiative verbs.

    Returns:
        The composed :class:`~persona_api.background.chat_turn_worker.ChatTurnRegistry`.
    """
    return ChatTurnRegistry(
        sink=sink,
        rls_engine=rls_engine,
        credits_policy=credits_policy,
        # Spec M4 T7b: Pro auto-top-up on a post-turn deduct crossing below $2 (off-loop).
        gateway=gateway,
        credits_per_turn=config.credits_per_turn,
        # Spec M2 (D-M2-5): proportional chat-turn billing (floor above);
        # PERSONA_API_PROPORTIONAL_CREDITS=false is the rollback hatch.
        proportional_credits=config.proportional_credits,
        # Spec M2 review (reviewer defense-in-depth, TAKE): the per-turn
        # charge sanity ceiling (PERSONA_API_MAX_TURN_CREDITS).
        max_turn_credits=config.max_turn_credits,
        # Spec M3 (T1b): the shared credit formula config (PERSONA_CREDIT_MARKUP,
        # default 1.0 → byte-identical charge; chat carries no per-call infra).
        billing_config=BillingConfig(),
        job_queue=job_queue,
        origination_service=origination_service,
        task_steering_service=task_steering_service,
        task_reschedule_service=task_reschedule_service,
        initiative_verb_service=initiative_verb_service,
    )
