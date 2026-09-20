"""Delivering an origination costs the owner the same whoever delivered it (R9-120, T4).

The ledger half of the condition attached to the 2026-09-21 ruling. The audit half is a
unit test (``packages/connectors/tests/unit/test_origination_parity_across_roots.py``);
credits live in Postgres behind RLS, so this half needs a real database and is marked
``integration``. Integration does not run locally by owner rule, so its evidence comes
from CI.

Two claims, and the first is the one that could have gone wrong quietly:

1. **Delivering an originated message charges nothing on its own**, on the web home and on
   a connector alike. Routing a message to Telegram instead of a web tab must not invent a
   charge, and a charge that appeared only on one of the two paths is exactly the drift the
   condition exists to catch. A positive control runs in the same test, because "no rows
   appeared" is worthless unless the same counting can see a row when there is one.
2. **Where delivery DOES cost money, it is the same row either way.** An outbound SMS
   segment is billed from the Twilio status callback keyed on the message SID, and
   ``SmsConnector.deliver`` reaches ``send`` with the same ``status_callback`` a reply
   does. So an originated SMS and a replied SMS produce the same ledger row shape through
   the same seam, which is asserted here rather than assumed from reading the code.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from persona.delivery import DeliveryOutcome
from persona.schema.origination import OriginatedMessage, PersonaIdentityTag
from persona_api.db.models import connector_conversations, conversations, personas
from persona_api.db.models import credit_transactions as credit_transactions_t
from persona_api.editions.credits_policy import MeteredCreditsPolicy
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from persona_api.services.background_billing import bill_background_provider
from persona_api.services.origination_delivery import ChannelDeliverers, build_origination_router
from sqlalchemy import func, insert, select, text

if TYPE_CHECKING:
    from collections.abc import Iterator

    from persona.delivery import DeliveryResult
    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

_OWNER = "user_ledger_parity"
_PERSONA = "persona_ledger_parity"
_WEB_CONVERSATION = "conv_ledger_web"
_TELEGRAM_CONVERSATION = "conv_ledger_telegram"
_NOW = datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)


class _RecordingTelegram:
    """A deliverer that reports delivered, so the ledger is the only variable."""

    def __init__(self) -> None:
        self.delivered: list[OriginatedMessage] = []

    async def deliver(self, message: OriginatedMessage) -> DeliveryResult:
        from persona.delivery import DeliveryResult as _Result

        self.delivered.append(message)
        return _Result(outcome=DeliveryOutcome.DELIVERED, channel="telegram")


@pytest.fixture
def seeded(migrated_engine: Engine, database_url: str) -> Iterator[tuple[Engine, Engine]]:
    """An owner with one web conversation and one Telegram conversation.

    Yields ``(rls_engine, superuser_engine)``: the first is what production originates on,
    the second is how the test reads the ledger without its own RLS scope getting in the
    way of the assertion.
    """
    with migrated_engine.begin() as conn:
        # The user row first: personas, conversations and credit_transactions all FK to it,
        # so a missing one fails as an integrity error in CI rather than as the assertion
        # this test is about.
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:u, :e)"),
            {"u": _OWNER, "e": f"{_OWNER}@example.com"},
        )
        conn.execute(
            insert(personas).values(id=_PERSONA, owner_id=_OWNER, yaml="identity:\n  name: Ada\n")
        )
        for conversation_id in (_WEB_CONVERSATION, _TELEGRAM_CONVERSATION):
            conn.execute(
                insert(conversations).values(
                    id=conversation_id, owner_id=_OWNER, persona_id=_PERSONA, title="t"
                )
            )
        conn.execute(
            insert(connector_conversations).values(
                id="cc_ledger",
                owner_id=_OWNER,
                platform="telegram",
                channel_key="5551230000",
                persona_id=_PERSONA,
                conversation_id=_TELEGRAM_CONVERSATION,
            )
        )
    rls_engine = make_rls_engine(database_url)
    token = current_user_id.set(_OWNER)
    try:
        yield rls_engine, migrated_engine
    finally:
        current_user_id.reset(token)
        rls_engine.dispose()


def _ledger_rows(superuser: Engine) -> int:
    with superuser.begin() as conn:
        return int(
            conn.execute(
                select(func.count())
                .select_from(credit_transactions_t)
                .where(credit_transactions_t.c.user_id == _OWNER)
            ).scalar_one()
        )


def _message(conversation_id: str) -> OriginatedMessage:
    return OriginatedMessage(
        persona=PersonaIdentityTag(persona_id=_PERSONA, display_name="Ada"),
        owner_user_id=_OWNER,
        content="I have something for you.",
        conversation_id=conversation_id,
        created_at=_NOW,
    )


@pytest.mark.asyncio
async def test_delivering_an_origination_charges_nothing_on_either_channel(
    seeded: tuple[Engine, Engine],
) -> None:
    """Routing to a connector must not invent a charge the web path does not have.

    The positive control at the end is the point of the test's shape: without it, a
    broken query or an unseeded owner would report "no new rows" forever and this would
    be a green that proves nothing.
    """
    rls_engine, superuser = seeded
    channels = ChannelDeliverers()
    telegram = _RecordingTelegram()
    channels.bind({"telegram": telegram})
    router = build_origination_router(rls_engine=rls_engine, channels=channels)

    before = _ledger_rows(superuser)

    web_result = await router.deliver(_message(_WEB_CONVERSATION))
    after_web = _ledger_rows(superuser)

    telegram_result = await router.deliver(_message(_TELEGRAM_CONVERSATION))
    after_telegram = _ledger_rows(superuser)

    # Both were really delivered somewhere, or "no charge" is true for the wrong reason.
    assert web_result.channel == "web"
    assert telegram_result.channel == "telegram"
    assert [m.conversation_id for m in telegram.delivered] == [_TELEGRAM_CONVERSATION]

    assert after_web == before, "delivering to the web home charged the owner"
    assert after_telegram == after_web, (
        "delivering the same message to a connector charged the owner and the web path "
        "did not; the two delivery paths have drifted on billing"
    )

    # The positive control: the same counting sees a real charge when one lands.
    bill_background_provider(
        credits_policy=MeteredCreditsPolicy(),
        rls_engine=rls_engine,
        owner_id=_OWNER,
        provider_cents=5.0,
        cost_basis="provider_meter",
        surface="sms_segments",
        billing_key="ledger-parity-control",
    )
    assert _ledger_rows(superuser) == after_telegram + 1, (
        "the ledger count cannot see a charge that did land, so the zeros above prove nothing"
    )


@pytest.mark.asyncio
async def test_an_originated_sms_bills_through_the_same_seam_as_a_reply(
    seeded: tuple[Engine, Engine],
) -> None:
    """The one channel where delivery costs money: same seam, same row, same key.

    An outbound SMS is billed from the Twilio status callback keyed on the message SID,
    and the origination path reaches ``send`` with the same ``status_callback`` a reply
    does. So the charge cannot depend on which produced the message, and the SID keying
    means a redelivered callback charges once. Both are asserted rather than read off the
    code, because "it goes through the same function" is the claim this whole condition
    exists to stop us assuming.
    """
    from persona_connectors.composition import build_sms_cost_biller

    rls_engine, superuser = seeded
    charge = build_sms_cost_biller(
        credits_policy=MeteredCreditsPolicy(),
        rls_engine=rls_engine,
        resolve_owner=lambda _number: _OWNER,
        price_per_segment_cents=0.79,
    )
    reply_callback = {
        "MessageSid": "SM_reply",
        "MessageStatus": "sent",
        "NumSegments": "2",
        "To": "+15551230000",
    }
    originated_callback = {**reply_callback, "MessageSid": "SM_originated"}

    before = _ledger_rows(superuser)
    charge(reply_callback)
    after_reply = _ledger_rows(superuser)
    charge(originated_callback)
    after_originated = _ledger_rows(superuser)

    assert after_reply == before + 1, "the reply's segments were not billed at all"
    assert after_originated == after_reply + 1, (
        "an originated SMS did not bill through the seam a reply bills through"
    )

    with superuser.begin() as conn:
        rows = {
            r["reason"]: r
            for r in conn.execute(
                select(
                    credit_transactions_t.c.reason,
                    credit_transactions_t.c.delta,
                    credit_transactions_t.c.cost_cents,
                    credit_transactions_t.c.cost_basis,
                )
                .where(credit_transactions_t.c.user_id == _OWNER)
                .order_by(credit_transactions_t.c.created_at)
            )
            .mappings()
            .all()
        }
    assert len(rows) == 1, (
        f"the two charges wrote different ledger reasons: {sorted(rows)}; an originated "
        "SMS and a replied SMS must be the same kind of row"
    )
    row = next(iter(rows.values()))
    assert row["cost_basis"] == "provider_meter"

    # Idempotent on the SID, so Twilio's queued/sent/delivered burst charges once.
    charge(reply_callback)
    assert _ledger_rows(superuser) == after_originated, (
        "a redelivered status callback charged the owner twice"
    )
