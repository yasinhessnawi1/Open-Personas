"""Clerk's ``user.deleted`` webhook starts the account's erasure (R9-284 T9, ruling 2).

Clerk signs its webhooks with Svix. The check is done here with the standard library rather than
the ``svix`` package, which is a full API client with six dependencies for what is one HMAC:
``v1`` signatures are base64 HMAC-SHA256, keyed by the base64 part of the ``whsec_`` secret,
over ``{svix-id}.{svix-timestamp}.{body}``; the header may carry several space-separated
signatures (a secret being rotated), any one of which may match; and a timestamp more than five
minutes from now is refused, so a captured delivery cannot be replayed later. The unit tests
check it against Svix's documented test vector.

A delivery for a user we never provisioned, or one already erased, is acknowledged and changes
nothing. The payload is never logged: it carries the user's personal data.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from persona.logging import get_logger
from sqlalchemy import select

from persona_api.db.models import users as users_t
from persona_api.errors import ClerkWebhookRejectedError
from persona_api.services.account_erasure import ORIGIN_CLERK, begin_account_erasure
from persona_api.services.erasure_ledger import ErasureLedger, ErasureState, SubjectKind

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from persona_api.services.erasure_ledger import Erasure

__all__ = ["MAX_BODY_BYTES", "TOLERANCE", "ClerkDeletion", "handle_event", "verify_signature"]

_log = get_logger("api.erasure")

#: A ``user.deleted`` delivery is a few hundred bytes; anything far larger is refused unread.
MAX_BODY_BYTES = 64 * 1024
#: How far a delivery's timestamp may be from now (Svix's own default).
TOLERANCE = timedelta(minutes=5)

_SECRET_PREFIX = "whsec_"
_USER_ID = re.compile(r"user_[A-Za-z0-9]{1,64}")


def _reject(reason: str) -> ClerkWebhookRejectedError:
    return ClerkWebhookRejectedError("the webhook was rejected", context={"reason": reason})


def verify_signature(
    secret: str,
    *,
    msg_id: str,
    timestamp: str,
    signatures: str,
    body: bytes,
    now: datetime | None = None,
) -> None:
    """Raise :class:`ClerkWebhookRejectedError` unless the delivery is Clerk's and recent."""
    if not secret.startswith(_SECRET_PREFIX):
        raise _reject("bad_secret")
    try:
        key = base64.b64decode(secret.removeprefix(_SECRET_PREFIX), validate=True)
    except (binascii.Error, ValueError):
        raise _reject("bad_secret") from None
    try:
        sent = datetime.fromtimestamp(int(timestamp), tz=UTC)
    except (ValueError, OverflowError, OSError):
        raise _reject("bad_timestamp") from None
    if abs((now or datetime.now(UTC)) - sent) > TOLERANCE:
        raise _reject("stale_timestamp")
    signed = msg_id.encode() + b"." + timestamp.encode() + b"." + body
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    for part in signatures.split(" "):
        version, _, signature = part.partition(",")
        if version == "v1" and hmac.compare_digest(signature, expected):
            return
    raise _reject("bad_signature")


@dataclass(frozen=True)
class ClerkDeletion:
    """What the delivery did: the erasure it started or found, or why it changed nothing."""

    erasure: Erasure | None
    outcome: str


def _user_id(body: bytes) -> str | None:
    """The deleted user's id, or ``None`` for another event type."""
    try:
        event = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise _reject("bad_json") from None
    if not isinstance(event, dict) or event.get("type") != "user.deleted":
        return None
    data = event.get("data")
    user_id = data.get("id") if isinstance(data, dict) else None
    if not isinstance(user_id, str) or not _USER_ID.fullmatch(user_id):
        raise _reject("bad_user_id")
    return user_id


def _known(engine: Engine, user_id: str) -> bool:
    with engine.connect() as conn:
        return conn.execute(select(users_t.c.id).where(users_t.c.id == user_id)).first() is not None


def handle_event(engine: Engine, body: bytes) -> ClerkDeletion:
    """Start (or find) the account's erasure for a verified ``user.deleted`` delivery."""
    user_id = _user_id(body)
    if user_id is None:
        return ClerkDeletion(None, "ignored")
    existing = ErasureLedger(engine).get(SubjectKind.ACCOUNT, user_id)
    if existing is not None and existing.state is not ErasureState.COMPLETE:
        return ClerkDeletion(existing, "running")
    if not _known(engine, user_id):
        # Never provisioned here, or already erased: nothing of theirs is left to erase.
        return ClerkDeletion(None, "unknown")
    erasure = begin_account_erasure(engine, owner_id=user_id, origin=ORIGIN_CLERK)
    _log.info("clerk user.deleted started an account erasure erasure={eid}", eid=erasure.id)
    return ClerkDeletion(erasure, "started")
