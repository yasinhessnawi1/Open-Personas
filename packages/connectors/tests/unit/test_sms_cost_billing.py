"""An outbound SMS segment is charged to the owner who sent it (M track, SMS cost truth).

Twilio bills per SEGMENT and a persona that answers at length sends several of them. The
count has been read off the delivery status callback since C4 T12, but the price it was
multiplied by was an optional keyword argument that no caller in the repository ever
passed, so ``SmsCost.cost`` could only ever be ``None``, the "recording" was a log line
with no reader, and every outbound SMS the product sent was paid for by the house.

Three things had to be established before this could be fixed and all three are asserted
here rather than assumed:

* the callback can be attributed to an owner (the destination number is the identity C1
  bound at link time, resolved through the same binding the inbound path uses);
* exactly one charge lands per message despite the queued/sent/delivered burst;
* a deployment with no configured price bills nobody and says so, because Twilio's
  per-segment price varies by destination country and a guessed price overcharges a
  person (the ``image_pricing`` precedent).

The last test is the composition half, and it is the one a behavioural test cannot cover:
the biller could be perfect while the composition root built it with no credits policy, or
while the connector sent messages that never asked Twilio for a status callback at all.
Both of those were true before this change.
"""

from __future__ import annotations

import ast
import contextlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from loguru import logger as _loguru_logger
from persona_connectors import service
from persona_connectors.composition import build_sms_cost_biller
from persona_connectors.errors import IdentityNotLinkedError

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

_PRICE_CENTS = 0.83  # US long-code outbound, the price an operator would configure


@contextlib.contextmanager
def _warnings() -> Iterator[list[str]]:
    """Capture WARNING-level loguru output for the duration of the block."""
    captured: list[str] = []
    sink_id = _loguru_logger.add(lambda msg: captured.append(str(msg)), level="WARNING")
    try:
        yield captured
    finally:
        _loguru_logger.remove(sink_id)


class _RecordingPolicy:
    """A credits policy that records what it was asked to charge."""

    def __init__(self) -> None:
        self.charges: list[dict[str, Any]] = []

    def capture_up_to_idempotent(self, **kw: Any) -> tuple[int, int]:  # noqa: ANN401
        self.charges.append(kw)
        return int(kw["amount"]), 100


def _biller(
    policy: _RecordingPolicy | None,
    *,
    price_cents: float | None = _PRICE_CENTS,
    owner: str | None = "owner-1",
) -> Any:  # noqa: ANN401 (the built closure)
    def resolve_owner(_number: str) -> str | None:
        return owner

    return build_sms_cost_biller(
        credits_policy=policy,  # type: ignore[arg-type]
        rls_engine=object() if policy is not None else None,  # type: ignore[arg-type]
        resolve_owner=resolve_owner,
        price_per_segment_cents=price_cents,
    )


def _callback(status: str = "sent", segments: str = "3", sid: str = "SM123") -> Mapping[str, str]:
    return {
        "MessageSid": sid,
        "MessageStatus": status,
        "NumSegments": segments,
        "To": "+15550001111",
        "From": "+15550002222",
    }


def test_an_outbound_sms_bills_its_segments_to_the_linked_owner() -> None:
    """The finding: the price was an argument nobody passed, so the cost was always None
    and nobody was ever charged for a message that cost real money."""
    policy = _RecordingPolicy()

    _biller(policy)(_callback())

    assert len(policy.charges) == 1, "the SMS segments were not billed"
    charge = policy.charges[0]
    assert charge["user_id"] == "owner-1"
    assert charge["cost_cents"] == pytest.approx(2.49)  # 3 segments × 0.83¢
    assert charge["cost_basis"] == "provider_meter"
    assert charge["reason"] == "sms_segments:provider_meter"
    assert charge["amount"] >= 3  # ceil(2.49) with the 1-credit floor below it


def test_the_idempotency_key_is_rooted_on_the_globally_unique_message_sid() -> None:
    """The conflict gate is unique on the billing key ALONE (R9-196), so the key has to be
    unique across every surface, not just within this one."""
    policy = _RecordingPolicy()

    _biller(policy)(_callback())

    assert policy.charges[0]["billing_key"] == "sms_segments:SM123"


def test_one_message_charges_once_across_its_callback_burst() -> None:
    """Twilio posts queued, then sent, then delivered for the SAME message. Only the send
    is a cost, and the two that follow it reuse the key so the conflict gate absorbs them."""
    policy = _RecordingPolicy()
    bill = _biller(policy)

    bill(_callback(status="queued"))
    bill(_callback(status="sent"))
    bill(_callback(status="delivered"))

    assert [c["billing_key"] for c in policy.charges] == [
        "sms_segments:SM123",
        "sms_segments:SM123",
    ], "the queued callback must not charge, and the rest must share one key"


def test_two_messages_get_two_keys() -> None:
    policy = _RecordingPolicy()
    bill = _biller(policy)

    bill(_callback(sid="SMaaa"))
    bill(_callback(sid="SMbbb"))

    assert {c["billing_key"] for c in policy.charges} == {
        "sms_segments:SMaaa",
        "sms_segments:SMbbb",
    }


def test_a_failed_send_is_not_charged() -> None:
    policy = _RecordingPolicy()

    _biller(policy)({**_callback(status="failed"), "ErrorCode": "30008"})

    assert policy.charges == []


def test_no_configured_price_bills_nobody_and_says_so() -> None:
    """The image-pricing precedent: undercharging costs the house money, a guessed price
    overcharges a person. So the gap is LOUD, not filled in. Loud is the load-bearing part
    here: the original defect survived for a year because an unpriced SMS logged a cost of
    ``None`` at INFO and looked exactly like a free one."""
    policy = _RecordingPolicy()

    with _warnings() as captured:
        _biller(policy, price_cents=None)(_callback())

    assert policy.charges == []
    assert any("PERSONA_CONNECTORS_SMS_PRICE_PER_SEGMENT_CENTS" in line for line in captured), (
        "an SMS we cannot price went unbilled with no signal naming the knob to set"
    )


def test_an_unattributable_message_bills_nobody_and_says_so() -> None:
    """The link can be revoked between the send and the callback. Nobody is charged, and
    nobody ELSE is charged either, which is the failure that would actually hurt."""
    policy = _RecordingPolicy()

    with _warnings() as captured:
        _biller(policy, owner=None)(_callback())

    assert policy.charges == []
    assert any("SM123" in line for line in captured)


def test_a_revoked_link_resolves_to_no_owner_rather_than_raising() -> None:
    """The exception-free shape the webhook needs: C1 raises, the status path must not,
    because raising here makes Twilio retry a message that was already delivered."""

    class _Linking:
        def resolve_owner(self, *, platform_identity: str) -> str:
            raise IdentityNotLinkedError("gone", context={"identity": platform_identity})

    assert service._resolve_phone_owner(_Linking(), "+15550001111") is None  # type: ignore[arg-type]


def test_billing_never_raises_into_the_webhook() -> None:
    """Fail-soft: the message is already gone by the time this runs. A raise here would
    make Twilio retry the status callback, not un-send the SMS."""

    class _ExplodingPolicy(_RecordingPolicy):
        def capture_up_to_idempotent(self, **kw: Any) -> tuple[int, int]:  # noqa: ANN401, ARG002
            msg = "ledger down"
            raise RuntimeError(msg)

    _biller(_ExplodingPolicy())(_callback())  # must not raise


def test_an_unmetered_install_charges_nothing() -> None:
    """Community / self-host: no credits policy leaves the status path as it was."""
    _biller(None)(_callback())  # must not raise


def test_a_garbage_callback_is_not_a_crash_and_not_a_charge() -> None:
    policy = _RecordingPolicy()

    _biller(policy)({"nonsense": "yes"})

    assert policy.charges == []


# ----- the composition half -----------------------------------------------------------


def _sms_setup_source() -> ast.FunctionDef:
    tree = ast.parse(Path(service.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_setup_sms":
            return node  # type: ignore[return-value]
    msg = "_setup_sms not found in the connector service composition root"
    raise AssertionError(msg)


def test_the_service_root_hands_the_sms_status_path_a_credits_policy() -> None:
    """The biller could be perfect and still bill nobody: every billing argument on the
    seam is keyword-only with a None default, which is exactly how R9-079 made every
    connector turn free. So the call site is asserted, not trusted."""
    tree = ast.parse(Path(service.__file__).read_text(encoding="utf-8"))

    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_setup_sms"
    ]

    assert len(calls) == 1
    passed = {kw.arg for kw in calls[0].keywords}
    assert {"credits_policy", "rls_engine"} <= passed, (
        "_setup_sms is called without the billing seam; the SMS status path cannot charge"
    )


def test_the_sms_status_handler_is_the_biller_and_gets_the_configured_price() -> None:
    """A log line with no reader is what the finding was. The status path must build the
    biller, with the price knob wired to it."""
    source = ast.dump(_sms_setup_source())

    assert "build_sms_cost_biller" in source, "the SMS status path does not bill"
    assert "sms_price_per_segment_cents" in source, "the configured price is not wired in"


def test_the_sms_connector_asks_twilio_for_the_status_callback() -> None:
    """Without this the whole path is dark. ``build_twilio_app`` has served
    ``/sms/status`` since T9, but nothing ever set ``StatusCallback`` on a send, so the
    route only fired if the number happened to be configured for it in the Twilio console.
    The cost is read off that callback, so the charge depends on the send requesting it.

    Asserted as a RESOLVED value, not as a keyword that merely appears: an earlier
    version of this test checked only that the name ``status_callback_url`` was
    somewhere in the source, and stayed green when the argument was neutered to an
    empty string, which is the realistic regression (a simplification, or a default
    that quietly goes blank) and leaves the path exactly as dark as it was before.
    """
    keywords = [
        kw
        for node in ast.walk(_sms_setup_source())
        if isinstance(node, ast.Call)
        for kw in node.keywords
        if kw.arg == "status_callback_url"
    ]

    assert keywords, (
        "the SMS connector is built without a status callback URL; no callback, no cost"
    )
    for kw in keywords:
        assert isinstance(kw.value, ast.Call), (
            "status_callback_url is passed a literal rather than the resolver; a blank "
            "or hardcoded value means Twilio never calls back and nothing is ever billed"
        )
        func = kw.value.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        assert name == "_status_callback_url", (
            f"status_callback_url comes from {name!r}, not the shared resolver that "
            "reads the configured base URL"
        )
