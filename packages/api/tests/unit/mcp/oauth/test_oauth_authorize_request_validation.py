"""R9-048 hardening — `MCPOAuthAuthorizeRequest.redirect_after` server-side validation.

Defense-in-depth: every CURRENT caller derives `redirect_after` from
`window.location.pathname + search` at click time (never attacker input), so
there is no live exploit today. But nothing previously stopped a future or
crafted caller from setting an absolute/external URL, which would open a
redirect once the OAuth dance completes. `redirect_after` must be a bare,
same-origin, relative in-app path — validated at the request-model boundary
so a bad value fails fast as a structured 422, not silently accepted and
handed to the client's `router.replace()` later.
"""

from __future__ import annotations

import pytest
from persona_api.schemas.requests import MCPOAuthAuthorizeRequest
from pydantic import ValidationError


class TestRedirectAfterAccepts:
    def test_none_is_the_default_and_valid(self) -> None:
        req = MCPOAuthAuthorizeRequest()
        assert req.redirect_after is None

    def test_a_simple_relative_path(self) -> None:
        req = MCPOAuthAuthorizeRequest(redirect_after="/personas/x")
        assert req.redirect_after == "/personas/x"

    def test_a_relative_path_with_query_string(self) -> None:
        req = MCPOAuthAuthorizeRequest(redirect_after="/personas/x?tab=apps")
        assert req.redirect_after == "/personas/x?tab=apps"

    def test_root_path(self) -> None:
        req = MCPOAuthAuthorizeRequest(redirect_after="/")
        assert req.redirect_after == "/"


class TestRedirectAfterRejects:
    @pytest.mark.parametrize(
        "bad_value",
        [
            "//evil.com",
            "https://evil.com",
            "http://evil.com",
            "javascript:alert(1)",
            "/\\evil.com",
            "",
            "evil.com",
            "personas/x",
            "\\\\evil.com",
            "/foo\nbar",
            "/foo\tbar",
        ],
    )
    def test_rejects_non_relative_or_dangerous_values(self, bad_value: str) -> None:
        with pytest.raises(ValidationError) as exc_info:
            MCPOAuthAuthorizeRequest(redirect_after=bad_value)
        assert "redirect_after" in str(exc_info.value)
