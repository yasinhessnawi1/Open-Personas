"""``POST /v1/webhooks/clerk``: Clerk tells us a user was deleted (R9-284 T9, ruling 2).

Cloud only. Unauthenticated by design (Clerk calls it); the Svix signature is the
authentication. In order: a body over :data:`MAX_BODY_BYTES` is refused unread (413), the
signature and timestamp are checked (400 on failure), and only then is the body parsed. A
``user.deleted`` starts the account's erasure with origin ``clerk`` (so the Clerk delete is
skipped) and hands it to the runner; other event types are acknowledged and ignored. The body is
never logged.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool

from persona_api.config import Edition
from persona_api.errors import AccountErasureUnavailableError, ClerkWebhookRejectedError
from persona_api.services.clerk_webhook import MAX_BODY_BYTES, handle_event, verify_signature

router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])


async def _bounded_body(request: Request) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > MAX_BODY_BYTES):
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, detail="body too large")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY_BYTES:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, detail="body too large")
    return bytes(body)


@router.post("/clerk")
async def clerk_webhook(request: Request) -> dict[str, str]:
    state: Any = request.app.state
    config = state.config
    if config.edition is not Edition.cloud:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    secret = config.clerk_webhook_secret
    runner = getattr(state, "account_erase_runner", None)
    platform = getattr(state, "platform_engine", None)
    if secret is None or runner is None or platform is None:
        # 503, so Clerk retries the delivery once this process can act on it.
        raise AccountErasureUnavailableError("account deletion is not available", context={})
    body = await _bounded_body(request)
    headers = request.headers
    msg_id = headers.get("svix-id")
    timestamp = headers.get("svix-timestamp")
    signatures = headers.get("svix-signature")
    if not msg_id or not timestamp or not signatures:
        raise ClerkWebhookRejectedError("the webhook was rejected", context={"reason": "headers"})
    verify_signature(
        secret.get_secret_value(),
        msg_id=msg_id,
        timestamp=timestamp,
        signatures=signatures,
        body=body,
    )
    deletion = await run_in_threadpool(handle_event, platform, body)
    if deletion.erasure is not None:
        runner.submit(deletion.erasure)
    return {"outcome": deletion.outcome}
