"""LLM-assisted authoring routes — SSE-streamed draft + refine (spec 10 + P0, D-10-2).

Drives the real app against Docker Postgres with a fake JWT verifier and a
*stubbed* tier registry returning a scripted backend (no real model call — that
is the @pytest.mark.external corpus eval, T06/T08). Asserts the spec-10 contract
PLUS the P0 streaming change: ``/author`` + ``/author/refine`` now SSE-stream
(``chunk`` … terminal ``draft`` … ``done``); the terminal draft is the same
validated ``AuthoringDraft`` (no contract change, D-10-6); creation stays on
``POST /v1/personas``; refine still rejects ``round >= 3`` BEFORE streaming; and
credits deduct ONLY after the terminal draft (D-P0-deduct-after-validate /
D-08-6) — a provider failure deducts nothing, a validation-exhausted draft still
charges (D-10-8), and the pre-flight 402 fires before any SSE frame (D-11-12).
"""

from __future__ import annotations

import contextlib
import json
import os
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from persona.backends.types import ChatResponse, StreamChunk, TokenUsage
from persona.errors import CreditsExhaustedError
from persona_api.app import create_app
from persona_api.auth import AuthenticatedUser
from persona_api.config import APIConfig
from persona_api.middleware.rls_context import make_rls_engine
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy import Engine
    from tests.conftest import HashEmbedder384

pytestmark = pytest.mark.integration

_DRAFT_RESPONSE = """\
schema_version: "1.0"
identity:
  name: Astrid
  role: Norwegian tenancy-law assistant
  background: Helps tenants understand husleieloven in plain language.
  language_default: nb
  constraints:
    - Do not fabricate information; say when you don't know.
    - Do not give binding legal advice; recommend a qualified lawyer.
self_facts:
  - fact: Specialises in the Norwegian Tenancy Act.
    confidence: 1.0
worldview:
  - claim: Most tenancy disputes are avoidable with a clear contract.
    domain: tenancy-law
    epistemic: belief
    confidence: 0.8
tools: []
skills: []
---QUESTIONS---
[{"section": "identity", "question": "Should Astrid serve tenants, landlords, or both?"}]"""

# Schema-invalid variant: a top-level `hobbies` key the schema's extra="forbid"
# rejects — injected into the YAML portion (before the questions marker). Used to
# force the validation-exhausted path (bad on both attempts).
_BAD_RESPONSE = (
    _DRAFT_RESPONSE.split("---QUESTIONS---")[0].rstrip()
    + "\nhobbies:\n  - chess\n---QUESTIONS---\n[]"
)


def _stream_of(content: str) -> object:
    """Build an async generator that streams ``content`` as two chunks (D-02-5 shape)."""

    async def _gen() -> object:
        mid = len(content) // 2
        yield StreamChunk(delta=content[:mid], is_final=False)
        yield StreamChunk(
            delta=content[mid:],
            is_final=True,
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )

    return _gen()


class _ScriptedBackend:
    """Streams the canned draft; ``chat`` kept for non-streaming callers (recommenders)."""

    #: Spec M3 (T2a): _price_usage reads provider/model. ``scripted`` is not in the
    #: static pricing table → the turn prices ``unpriced`` (0.0¢) and the authoring
    #: charge falls to the floor — the "balance decreased" billing tests still hold.
    provider_name = "scripted"
    model_name = "scripted"

    def chat_stream(self, messages: list, **_kwargs: object) -> object:  # noqa: ARG002
        return _stream_of(_DRAFT_RESPONSE)

    async def chat(self, messages: list, **_kwargs: object) -> ChatResponse:  # noqa: ARG002
        return ChatResponse(
            content=_DRAFT_RESPONSE,
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            model="scripted",
            provider="scripted",
            latency_ms=0.0,
        )


class _BadYamlStreamBackend:
    """Streams schema-invalid YAML on BOTH attempts → validation-exhausted draft."""

    provider_name = "scripted"
    model_name = "scripted"

    def chat_stream(self, messages: list, **_kwargs: object) -> object:  # noqa: ARG002
        return _stream_of(_BAD_RESPONSE)


class _RaisingStreamBackend:
    """``chat_stream`` raises mid-iteration — a provider failure (no terminal draft)."""

    provider_name = "scripted"
    model_name = "scripted"

    def chat_stream(self, messages: list, **_kwargs: object) -> object:  # noqa: ARG002
        async def _gen() -> object:
            raise RuntimeError("provider down")
            yield  # pragma: no cover — unreachable; makes _gen an async generator

        return _gen()


class _NoCreditsPolicy:
    """Pre-flight raises 402 → proves the guard fires BEFORE any SSE frame."""

    def require_credits(self, *, rls_engine: object, user_id: str) -> int:  # noqa: ARG002
        raise CreditsExhaustedError("insufficient credits")

    def deduct(self, **_kwargs: object) -> int:
        # Never reached (require_credits raises first); signature-flexible stub.
        return 0


class _StubRegistry:
    def __init__(self, backend: _ScriptedBackend) -> None:
        self._backend = backend

    def get(self, tier_name: str) -> _ScriptedBackend:  # noqa: ARG002
        return self._backend

    @property
    def configured_tier_names(self) -> tuple[str, ...]:
        # Mirrors the real TierRegistry surface so the persona-detail
        # capabilities hydrator (PersonaCapabilities) does not AttributeError.
        return ("frontier", "mid", "small")

    def supports_vision_for(self, tier_name: str) -> bool:  # noqa: ARG002
        # The scripted backend is text-only; mirror that so image-bearing
        # turns would route correctly if they reached this path.
        return False


@pytest.fixture
def client(
    migrated_engine: Engine,  # noqa: ARG001 — ensures schema + persona_app grants
    embedder: HashEmbedder384,
    tmp_path: Path,
) -> Iterator[tuple[TestClient, str]]:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")
    cfg = APIConfig(app_database_url=app_url, audit_root=str(tmp_path / "audit"))
    app = create_app(cfg)

    async def _fake_verify(token: str) -> AuthenticatedUser:
        return AuthenticatedUser(id=token, email=None)

    user_id = "user_t10"
    with TestClient(app) as c:
        app.state.verify_token = _fake_verify
        app.state.embedder = embedder
        app.state.tier_registry = _StubRegistry(_ScriptedBackend())
        app.state.authoring_tier = "frontier"
        su = make_rls_engine(os.environ["DATABASE_URL"])
        with su.begin() as conn:
            conn.execute(
                text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
                {"i": user_id, "e": f"{user_id}@x.test"},
            )
        su.dispose()
        yield c, user_id
        su = make_rls_engine(os.environ["DATABASE_URL"])
        with su.begin() as conn:
            conn.execute(text("DELETE FROM users WHERE id = :i"), {"i": user_id})
        su.dispose()


def _auth(user_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {user_id}"}


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    """Parse the buffered SSE response text into ``(event, data)`` frames."""
    events: list[tuple[str, dict]] = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        event = "message"
        data_lines: list[str] = []
        for line in block.split("\n"):
            if line.startswith("event:"):
                event = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:") :].lstrip())
        if data_lines:
            events.append((event, json.loads("\n".join(data_lines))))
    return events


def _author_stream(c: TestClient, uid: str, description: str) -> list[tuple[str, dict]]:
    resp = c.post("/v1/personas/author", json={"description": description}, headers=_auth(uid))
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")
    return _parse_sse(resp.text)


def _terminal_draft(events: list[tuple[str, dict]]) -> dict:
    drafts = [data for ev, data in events if ev == "draft"]
    assert len(drafts) == 1, f"expected exactly one terminal draft, got {len(drafts)}"
    return drafts[0]


def _balance(c: TestClient, uid: str) -> int:
    return int(c.get("/v1/me/credits", headers=_auth(uid)).json()["balance"])


def test_author_streams_chunks_then_terminal_draft(client: tuple[TestClient, str]) -> None:
    c, uid = client
    events = _author_stream(c, uid, "a Norwegian legal assistant focused on tenancy law")
    kinds = [ev for ev, _ in events]
    assert "chunk" in kinds  # the persona forms live (TTFT)
    assert kinds[-2:] == ["draft", "done"]  # terminal draft, then the done sentinel
    draft = _terminal_draft(events)
    assert draft["yaml"].startswith("schema_version:")
    assert draft["prompt_version"]  # acceptance #8: version on the terminal payload
    assert len(draft["questions"]) == 1
    assert draft["questions"][0]["section"] == "identity"
    assert draft["errors"] is None


def test_author_creates_no_persona_row(client: tuple[TestClient, str]) -> None:
    c, uid = client
    _author_stream(c, uid, "a tenancy assistant")
    # streaming a draft creates nothing — the list stays empty until an explicit save
    listed = c.get("/v1/personas", headers=_auth(uid)).json()
    assert listed == []


def test_author_then_save_creates_the_persona(client: tuple[TestClient, str]) -> None:
    c, uid = client
    draft = _terminal_draft(_author_stream(c, uid, "a tenancy assistant"))
    created = c.post("/v1/personas", json={"yaml": draft["yaml"]}, headers=_auth(uid))
    assert created.status_code == 201, created.text
    assert "Astrid" in created.json()["yaml"]


def test_author_deducts_a_credit_after_terminal_draft(client: tuple[TestClient, str]) -> None:
    c, uid = client
    before = _balance(c, uid)
    _author_stream(c, uid, "a tenancy assistant")  # consume through the terminal draft
    assert _balance(c, uid) < before


def test_author_no_charge_on_provider_failure(client: tuple[TestClient, str]) -> None:
    # A provider error mid-stream yields no terminal draft → the deduct (which
    # fires only on the draft event) never runs (D-P0-deduct-after-validate / D-08-6).
    c, uid = client
    c.app.state.tier_registry = _StubRegistry(_RaisingStreamBackend())  # type: ignore[arg-type]
    before = _balance(c, uid)
    # The stream errors after headers ship; the balance is the real assertion.
    with contextlib.suppress(Exception):
        c.post("/v1/personas/author", json={"description": "x"}, headers=_auth(uid))
    assert _balance(c, uid) == before


class _PriceableStreamBackend:
    """Streams the canned draft AND reports a priceable served model + real usage.

    Spec M3 (T2b): ``anthropic`` / ``claude-sonnet-5`` is in the static pricing
    table (0.30 / 1.50 ¢ per 1k tok), so 2000 prompt + 800 completion tokens cost
    ``2·0.30 + 0.8·1.50 = 1.8¢`` → ``ceil(1.8) = 2`` credits at MARKUP 1.0 (infra
    via the floor). The charge is observable as the balance delta.
    """

    provider_name = "anthropic"
    model_name = "claude-sonnet-5"

    def chat_stream(self, messages: list, **_kwargs: object) -> object:  # noqa: ARG002
        async def _gen() -> object:
            mid = len(_DRAFT_RESPONSE) // 2
            yield StreamChunk(delta=_DRAFT_RESPONSE[:mid], is_final=False)
            yield StreamChunk(
                delta=_DRAFT_RESPONSE[mid:],
                is_final=True,
                usage=TokenUsage(prompt_tokens=2000, completion_tokens=800, total_tokens=2800),
            )

        return _gen()


def test_author_charges_the_real_token_cost_not_a_flat_fee(
    client: tuple[TestClient, str],
) -> None:
    # Spec M3 (T2b, D-M3-9): the charge is the REAL Sonnet-5 token cost through the
    # credit formula (max(floor, ceil(MARKUP × cost))), NOT the retired flat 1000.
    # 1.8¢ → 2 credits; observable as the balance delta.
    c, uid = client
    c.app.state.tier_registry = _StubRegistry(_PriceableStreamBackend())  # type: ignore[arg-type]
    before = _balance(c, uid)
    _author_stream(c, uid, "a tenancy assistant")
    assert before - _balance(c, uid) == 2  # ceil(1.8), not 1000


def test_author_validation_exhausted_still_charges(client: tuple[TestClient, str]) -> None:
    # The counterpart distinction: a bad-on-both-attempts draft is STILL a
    # delivered draft (best-effort YAML + errors) and DOES charge (D-10-8).
    c, uid = client
    c.app.state.tier_registry = _StubRegistry(_BadYamlStreamBackend())  # type: ignore[arg-type]
    before = _balance(c, uid)
    draft = _terminal_draft(_author_stream(c, uid, "x"))
    assert draft["errors"]
    assert any("hobbies" in err for err in draft["errors"])
    assert _balance(c, uid) < before


def test_author_preflight_402_before_streaming(client: tuple[TestClient, str]) -> None:
    # The pre-flight 402 must fire BEFORE any SSE frame (D-11-12): raising inside
    # the generator after headers ship would give "response already started".
    c, uid = client
    c.app.state.credits_policy = _NoCreditsPolicy()
    resp = c.post("/v1/personas/author", json={"description": "x"}, headers=_auth(uid))
    assert resp.status_code == 402
    assert resp.json()["error"] == "credits_exhausted"
    assert "event:" not in resp.text  # a clean JSON 402, not a partial stream


def test_refine_streams_updated_draft(client: tuple[TestClient, str]) -> None:
    c, uid = client
    resp = c.post(
        "/v1/personas/author/refine",
        json={
            "current_yaml": _DRAFT_RESPONSE.split("---QUESTIONS---")[0].strip(),
            "question": "Should Astrid serve tenants, landlords, or both?",
            "answer": "Tenants.",
            "round": 0,
        },
        headers=_auth(uid),
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")
    draft = _terminal_draft(_parse_sse(resp.text))
    assert draft["yaml"].startswith("schema_version:")


def test_refine_rejects_round_over_cap_before_streaming(client: tuple[TestClient, str]) -> None:
    c, uid = client
    resp = c.post(
        "/v1/personas/author/refine",
        json={"current_yaml": "schema_version: '1.0'", "question": "q", "answer": "a", "round": 3},
        headers=_auth(uid),
    )
    assert resp.status_code == 422  # backstop fires before any SSE frame
    assert resp.json()["error"] == "refinement_limit_exceeded"
    assert "event:" not in resp.text
