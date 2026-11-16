"""``POST /v1/me/erase``: the user deletes their account (R9-284 T9, D-R9-284-erase ruling 2).

Cloud only (D-R9-284-account-erase). Immediate, no grace period (ruling 3): the request claims
the account's erasure, which fences the id at once (every later request from it answers 410),
and hands it to the account erasure runner; the answer is 202 with the erasure id. The caller
confirms by typing the account's email, or the phrase when the account has no real email. A
repeat while the erasure runs cannot arrive: the fence refuses it first.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from persona_api.auth import AuthenticatedUser, get_current_user
from persona_api.config import Edition
from persona_api.errors import AccountEraseConfirmationError, AccountErasureUnavailableError
from persona_api.services.account_erasure import ORIGIN_ROUTE, begin_account_erasure
from persona_api.services.user_service import get_user_profile

router = APIRouter(prefix="/v1/me", tags=["me"])

#: What the caller types when the account has no real email (the provider gave none).
CONFIRM_PHRASE = "delete my account"
_NOREPLY = "@users.noreply"


class AccountEraseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: str = Field(min_length=1, max_length=320)


class AccountEraseResponse(BaseModel):
    erasure_id: str
    state: str


class AccountEraseConfirmation(BaseModel):
    """What the caller must type to confirm (review M5): the stored email, or the phrase."""

    confirm: str


def _stored_email(platform: Any, user_id: str) -> str | None:  # noqa: ANN401 - an Engine
    profile = get_user_profile(platform, user_id=user_id)
    email = None if profile is None else profile.get("email")
    return email if isinstance(email, str) else None


def _platform_or_404(request: Request) -> Any:  # noqa: ANN401 - an Engine
    state: Any = request.app.state
    if state.config.edition is not Edition.cloud:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    platform = getattr(state, "platform_engine", None)
    if platform is None:
        raise AccountErasureUnavailableError("account deletion is not available", context={})
    return platform


@router.get("/erase", response_model=AccountEraseConfirmation)
def account_erase_confirmation(  # sync: the profile read blocks
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> AccountEraseConfirmation:
    """The confirmation the delete expects, read from the account as stored here.

    The web shows this rather than the identity provider's primary email, which can differ
    from the email this account was provisioned with.
    """
    platform = _platform_or_404(request)
    return AccountEraseConfirmation(
        confirm=_expected_confirmation(_stored_email(platform, user.id))
    )


def _expected_confirmation(email: str | None) -> str:
    if not email or email.endswith(_NOREPLY):
        return CONFIRM_PHRASE
    return email.strip().lower()


@router.post("/erase", status_code=status.HTTP_202_ACCEPTED, response_model=AccountEraseResponse)
def erase_account(  # sync: the claim and the profile read block, so it runs in the threadpool
    request: Request,
    body: AccountEraseRequest,
    user: AuthenticatedUser = Depends(get_current_user),
) -> AccountEraseResponse:
    """Delete the caller's account and everything in it. 202; the work runs in the background."""
    state: Any = request.app.state
    if state.config.edition is not Edition.cloud:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    runner = getattr(state, "account_erase_runner", None)
    platform = getattr(state, "platform_engine", None)
    if runner is None or platform is None:
        raise AccountErasureUnavailableError("account deletion is not available", context={})
    if body.confirm.strip().lower() != _expected_confirmation(_stored_email(platform, user.id)):
        raise AccountEraseConfirmationError("the confirmation does not match", context={})
    if runner.unready_reason(owner_id=user.id, origin=ORIGIN_ROUTE) is not None:
        # Review H2: never accept a deletion the runner would only fail to finish.
        raise AccountErasureUnavailableError("account deletion is not available", context={})
    erasure = begin_account_erasure(platform, owner_id=user.id, origin=ORIGIN_ROUTE)
    runner.submit(erasure)
    return AccountEraseResponse(erasure_id=erasure.id, state=erasure.state.value)
