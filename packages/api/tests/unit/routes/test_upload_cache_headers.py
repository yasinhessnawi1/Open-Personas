"""A content-addressed workspace file is served as immutable (issue #9).

Persona avatars live behind the Bearer-authed uploads route, so the web cannot
use a plain ``<img src>``: it fetches the bytes with a token and renders an
object URL. Without a caching promise from the API, that fetch went back to the
server on every navigation and every list render, and the portrait visibly
popped in after the initials mark.

The refs are ``blake2b`` digests of the bytes themselves, so they can never
point at different content: a regenerated avatar is a new ref. These pin the
header that lets the browser keep them, and pin that the promise is only made
for refs whose name actually carries the hash.
"""

# ruff: noqa: ARG001 - the stubs must mirror the real keyword signatures
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.responses import Response
from persona_api.routes import uploads as uploads_routes

_OWNER = "u_cache"
_PERSONA = "persona_cache"
#: blake2b(digest_size=16) renders as 32 hex characters.
_HASHED_REF = "uploads/0123456789abcdef0123456789abcdef.png"


def _request() -> SimpleNamespace:
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(file_storage=object())),
        state=SimpleNamespace(),
    )


@pytest.fixture(autouse=True)
def _stub_route(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the real route body; stub only its DB and disk edges."""

    def _visible(request: object, persona_id: str) -> None:
        return None

    def _fetch(**kwargs: object) -> tuple[bytes, str]:
        return b"\x89PNG bytes", "image/png"

    monkeypatch.setattr(uploads_routes, "_ensure_persona_visible", _visible)
    monkeypatch.setattr(uploads_routes.image_service, "fetch", _fetch)


async def _serve(ref: str) -> Response:
    return await uploads_routes.get_upload(
        persona_id=_PERSONA,
        ref=ref,
        request=_request(),  # type: ignore[arg-type]
        user=SimpleNamespace(id=_OWNER),  # type: ignore[arg-type]
    )


def test_a_hashed_ref_names_content_that_cannot_change() -> None:
    """The predicate is the whole safety argument for a year-long max-age."""
    assert uploads_routes.is_content_addressed_ref(_HASHED_REF)
    assert uploads_routes.is_content_addressed_ref("0123456789abcdef0123456789abcdef.webp")
    # A human-named upload makes no such promise, and neither does a near miss.
    assert not uploads_routes.is_content_addressed_ref("uploads/holiday-photo.png")
    assert not uploads_routes.is_content_addressed_ref("uploads/0123456789abcdef.png")
    assert not uploads_routes.is_content_addressed_ref(
        "uploads/0123456789ABCDEF0123456789ABCDEF.png"
    )


@pytest.mark.asyncio
async def test_an_avatar_is_served_immutable_so_the_browser_keeps_it() -> None:
    response = await _serve(_HASHED_REF)
    assert response.headers["Cache-Control"] == "private, max-age=31536000, immutable"
    # The existing promise is untouched.
    assert response.headers["X-Content-Type-Options"] == "nosniff"


@pytest.mark.asyncio
async def test_a_non_hashed_ref_gets_no_caching_promise() -> None:
    response = await _serve("uploads/holiday-photo.png")
    assert "Cache-Control" not in response.headers
