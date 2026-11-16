"""The Clerk user delete, over a mock transport (R9-284 T9). Nothing reaches Clerk."""

from __future__ import annotations

import httpx
import pytest
from persona_api.errors import ClerkCallError
from persona_api.services.clerk_users import CLERK_API, ClerkUsers


def _clerk(status: int, seen: list[httpx.Request]) -> ClerkUsers:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json={"deleted": status == 200})

    return ClerkUsers("sk_test_secret", client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_a_delete_is_a_delete_of_that_user_with_the_secret_key() -> None:
    seen: list[httpx.Request] = []

    assert _clerk(200, seen).delete_user("user_2abcDEF") is True

    (request,) = seen
    assert request.method == "DELETE"
    assert str(request.url) == f"{CLERK_API}/users/user_2abcDEF"
    assert request.headers["Authorization"] == "Bearer sk_test_secret"


def test_already_gone_counts_as_done() -> None:
    assert _clerk(404, []).delete_user("user_2abcDEF") is False


def test_another_failure_raises_with_the_status_only() -> None:
    with pytest.raises(ClerkCallError) as raised:
        _clerk(500, []).delete_user("user_2abcDEF")

    assert raised.value.context == {"status": "500"}


@pytest.mark.parametrize("bad", ["", "user_", "user_a/../b", "persona_x", "user_a b"])
def test_an_id_that_is_not_a_clerk_user_id_never_reaches_clerk(bad: str) -> None:
    seen: list[httpx.Request] = []

    with pytest.raises(ClerkCallError):
        _clerk(200, seen).delete_user(bad)

    assert seen == []
