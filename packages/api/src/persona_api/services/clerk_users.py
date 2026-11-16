"""Deleting a user in Clerk for an account erased through our route (R9-284 T9).

Clerk's Backend API, ``DELETE /v1/users/{id}`` with the instance's secret key. A 404 means the
user is already gone, which is what the erasure wants, so it counts as done; any other failure
raises with the status code only (a response body may carry personal data and is never logged).
"""

from __future__ import annotations

import re

import httpx
from persona.logging import get_logger

from persona_api.errors import ClerkCallError

__all__ = ["CLERK_API", "ClerkUsers"]

CLERK_API = "https://api.clerk.com/v1"

# A Clerk user id: ``user_`` and an alphanumeric tail. Anything else is refused before a request
# is built, so an id can never steer the path.
_USER_ID = re.compile(r"user_[A-Za-z0-9]{1,64}")

_log = get_logger("api.erasure")


class ClerkUsers:
    """The one Clerk call the account erasure makes."""

    def __init__(
        self, secret_key: str, *, client: httpx.Client | None = None, timeout: float = 10.0
    ) -> None:
        self._secret_key = secret_key
        self._client = client or httpx.Client(timeout=timeout)

    def delete_user(self, user_id: str) -> bool:
        """Delete the user; ``True`` if Clerk deleted it now, ``False`` if it was already gone."""
        if not _USER_ID.fullmatch(user_id):
            raise ClerkCallError("not a Clerk user id", context={"status": "refused"})
        response = self._client.delete(
            f"{CLERK_API}/users/{user_id}",
            headers={"Authorization": f"Bearer {self._secret_key}"},
        )
        if response.status_code == httpx.codes.NOT_FOUND:
            _log.info("erasure: clerk user already gone")
            return False
        if response.is_success:
            return True
        raise ClerkCallError(
            "clerk refused the user delete", context={"status": str(response.status_code)}
        )
