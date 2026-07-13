"""Unit tests for the build-time voice auto-assignment service (Issue 1).

Covers the model-pick parsing, the small-tier selection seam, the cross-service
catalogue fetch (forwarding the caller's bearer + language), and the fail-soft
orchestration: feature-off, builder-already-chose, and the happy path that
persists the picked voice.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Awaitable, Callable, Iterator
from types import SimpleNamespace

import httpx
import pytest
from loguru import logger as _loguru_logger
from persona.schema.conversation import ConversationMessage
from persona.schema.persona import Persona, PersonaIdentity
from persona_api.services import voice_assignment_service as vas


def _option(
    voice_id: str, gender: str, *, name: str = "", description: str = ""
) -> vas._VoiceOption:
    return vas._VoiceOption(voice_id=voice_id, name=name, gender=gender, description=description)


def _persona(*, voice: str | None = None, language: str = "en") -> Persona:
    return Persona(
        persona_id="persona_x",
        owner_id="owner_x",
        identity=PersonaIdentity(
            name="Ally",
            role="warm companion",
            background="A warm, supportive friend.",
            language_default=language,
            voice=voice,  # type: ignore[arg-type] — normalised from the shorthand
        ),
    )


class _FakeBackend:
    """Records the prompt and returns a canned reply for choose_voice."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.seen: list[ConversationMessage] | None = None

    async def chat(self, messages: list[ConversationMessage], **_: object) -> SimpleNamespace:
        self.seen = messages
        return SimpleNamespace(content=self.reply)


def _request(
    *,
    config: SimpleNamespace,
    tier_registry: SimpleNamespace,
    rls_engine: object,
    bearer: str | None,
) -> SimpleNamespace:
    headers = {"authorization": bearer} if bearer else {}
    state = SimpleNamespace(config=config, tier_registry=tier_registry, rls_engine=rls_engine)
    return SimpleNamespace(app=SimpleNamespace(state=state), headers=headers)


def _aret(value: object) -> Callable[..., Awaitable[object]]:
    async def _f(*_: object, **__: object) -> object:
        return value

    return _f


def _araise() -> Callable[..., Awaitable[object]]:
    async def _f(*_: object, **__: object) -> object:
        raise AssertionError("should not be called")

    return _f


# ----- _extract_choice -----------------------------------------------------


class TestExtractChoice:
    def test_exact_match(self) -> None:
        assert vas._extract_choice("v2", ["v1", "v2"]) == "v2"

    def test_substring_match_through_prose(self) -> None:
        assert vas._extract_choice('I choose "v1" for her.', ["v1", "v2"]) == "v1"

    def test_no_known_voice_returns_none(self) -> None:
        assert vas._extract_choice("none of these", ["v1", "v2"]) is None


# ----- choose_voice --------------------------------------------------------


class TestChooseVoice:
    def test_returns_backend_pick_and_passes_gender(self) -> None:
        backend = _FakeBackend("v1")
        options = [_option("v1", "feminine", name="Clara"), _option("v2", "masculine", name="Sam")]
        choice = asyncio.run(vas.choose_voice(persona=_persona(), backend=backend, options=options))
        assert choice == "v1"
        # The voice genders reach the model so it can match the persona.
        prompt = "\n".join(m.content for m in (backend.seen or []) if isinstance(m.content, str))
        assert "feminine" in prompt
        assert "masculine" in prompt

    def test_unusable_reply_returns_none(self) -> None:
        backend = _FakeBackend("I cannot decide")
        choice = asyncio.run(
            vas.choose_voice(
                persona=_persona(), backend=backend, options=[_option("v1", "feminine")]
            )
        )
        assert choice is None

    def test_empty_catalogue_returns_none(self) -> None:
        choice = asyncio.run(
            vas.choose_voice(persona=_persona(), backend=_FakeBackend("v1"), options=[])
        )
        assert choice is None

    def test_gender_mismatch_is_corrected_to_inferred_gender(self) -> None:
        # The model inferred feminine but picked a masculine voice — snap to a
        # feminine voice (the catalogue gender is the source of truth).
        backend = _FakeBackend("GENDER: feminine\nVOICE: v2")
        options = [
            _option("v1", "masculine"),
            _option("v2", "masculine"),
            _option("v3", "feminine"),
        ]
        choice = asyncio.run(vas.choose_voice(persona=_persona(), backend=backend, options=options))
        assert choice == "v3"

    def test_gender_match_keeps_the_models_pick(self) -> None:
        backend = _FakeBackend("GENDER: masculine\nVOICE: v2")
        options = [
            _option("v1", "feminine"),
            _option("v2", "masculine"),
            _option("v3", "masculine"),
        ]
        choice = asyncio.run(vas.choose_voice(persona=_persona(), backend=backend, options=options))
        assert choice == "v2"  # already masculine → not overridden

    def test_unknown_gender_leaves_the_pick_unconstrained(self) -> None:
        backend = _FakeBackend("GENDER: unknown\nVOICE: v1")
        options = [_option("v1", "feminine"), _option("v2", "masculine")]
        choice = asyncio.run(vas.choose_voice(persona=_persona(), backend=backend, options=options))
        assert choice == "v1"


# ----- _fetch_catalogue ----------------------------------------------------


class TestFetchCatalogue:
    def test_parses_and_forwards_bearer_and_language(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers.get("authorization")
            seen["language"] = request.url.params.get("language")
            seen["path"] = request.url.path
            return httpx.Response(
                200,
                json={
                    "provider": "cartesia",
                    "voices": [
                        {"voice_id": "v1", "name": "Clara", "gender": "feminine", "desc": "warm"},
                        {
                            "voice_id": "v2",
                            "name": "Sam",
                            "gender": "masculine",
                            "description": None,
                        },
                        {"name": "no id — skipped"},
                    ],
                },
            )

        transport = httpx.MockTransport(handler)
        monkeypatch.setattr(
            vas.httpx, "AsyncClient", functools.partial(httpx.AsyncClient, transport=transport)
        )

        provider, options = asyncio.run(
            vas._fetch_catalogue("http://voice/", bearer="Bearer tok", language="en")
        )
        assert provider == "cartesia"
        assert [o.voice_id for o in options] == ["v1", "v2"]  # the id-less entry is skipped
        assert options[1].description == ""  # None coerced to ""
        assert seen == {"auth": "Bearer tok", "language": "en", "path": "/v1/voices"}


# ----- maybe_assign_voice (orchestration) ----------------------------------


_YAML = (
    "schema_version: '1.0'\n"
    "identity:\n"
    "  name: Ally\n"
    "  role: warm companion\n"
    "  background: A warm, supportive friend.\n"
    "  language_default: en\n"
)
_YAML_WITH_VOICE = _YAML + "  voice: cartesia:existing-voice\n"


class TestMaybeAssignVoice:
    def test_feature_off_is_a_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(vas, "_fetch_catalogue", _araise())
        called: dict[str, object] = {}
        monkeypatch.setattr(vas.persona_service, "set_voice", lambda **k: called.update(k))
        request = _request(
            config=SimpleNamespace(voice_service_url="", voice_pick_tier="small"),
            tier_registry=SimpleNamespace(get=lambda _t: _FakeBackend("v1")),
            rls_engine=object(),
            bearer="Bearer t",
        )
        asyncio.run(vas.maybe_assign_voice(request, owner_id="o", persona_id="p", yaml_str=_YAML))
        assert called == {}

    def test_builder_voice_is_never_overridden(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(vas, "_fetch_catalogue", _araise())  # never fetched
        called: dict[str, object] = {}
        monkeypatch.setattr(vas.persona_service, "set_voice", lambda **k: called.update(k))
        request = _request(
            config=SimpleNamespace(voice_service_url="http://voice", voice_pick_tier="small"),
            tier_registry=SimpleNamespace(get=lambda _t: _FakeBackend("v1")),
            rls_engine=object(),
            bearer="Bearer t",
        )
        asyncio.run(
            vas.maybe_assign_voice(request, owner_id="o", persona_id="p", yaml_str=_YAML_WITH_VOICE)
        )
        assert called == {}

    def test_happy_path_persists_the_pick(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            vas,
            "_fetch_catalogue",
            _aret(("cartesia", [_option("v1", "feminine"), _option("v2", "masculine")])),
        )
        called: dict[str, object] = {}
        monkeypatch.setattr(vas.persona_service, "set_voice", lambda **k: called.update(k))
        engine = object()
        request = _request(
            config=SimpleNamespace(voice_service_url="http://voice", voice_pick_tier="small"),
            tier_registry=SimpleNamespace(get=lambda _t: _FakeBackend("v1")),
            rls_engine=engine,
            bearer="Bearer t",
        )
        asyncio.run(vas.maybe_assign_voice(request, owner_id="o", persona_id="p", yaml_str=_YAML))
        assert called == {
            "rls_engine": engine,
            "persona_id": "p",
            "provider": "cartesia",
            "voice_id": "v1",
        }

    def test_empty_catalogue_keeps_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(vas, "_fetch_catalogue", _aret((None, [])))
        called: dict[str, object] = {}
        monkeypatch.setattr(vas.persona_service, "set_voice", lambda **k: called.update(k))
        request = _request(
            config=SimpleNamespace(voice_service_url="http://voice", voice_pick_tier="small"),
            tier_registry=SimpleNamespace(get=lambda _t: _FakeBackend("v1")),
            rls_engine=object(),
            bearer="Bearer t",
        )
        asyncio.run(vas.maybe_assign_voice(request, owner_id="o", persona_id="p", yaml_str=_YAML))
        assert called == {}


# ----- catalogue-fetch failure logging (R9-025 reopen leg A) ---------------


@pytest.fixture
def loguru_capture() -> Iterator[list[str]]:
    """Loguru sink capturing every emitted message string (>= WARNING).

    ``persona.logging.get_logger`` wraps loguru, so pytest's stdlib-only
    ``caplog`` does not see records — mirrors
    ``test_chat_turn_worker_proportional.py``'s own fixture.
    """
    captured: list[str] = []
    sink_id = _loguru_logger.add(lambda msg: captured.append(str(msg)), level="WARNING")
    try:
        yield captured
    finally:
        _loguru_logger.remove(sink_id)


def _araise_http_status_error(response: httpx.Response) -> Callable[..., Awaitable[object]]:
    async def _f(*_: object, **__: object) -> object:
        raise httpx.HTTPStatusError(
            "upstream rejected", request=response.request, response=response
        )

    return _f


class TestCatalogueFetchFailureLogging:
    """R9-025 reopen leg A — the months-silent-failure lesson: this exact
    fail-soft catch swallowed the SAME auth misconfiguration the tts/stt
    proxies hit (a 401 on the forwarded bearer) at INFO, so personas kept
    coming out voiceless with nobody noticing. Now WARNING, with the upstream
    status pulled out explicitly (grep/alert-able, not buried in a repr)."""

    def test_logs_at_warning_not_info(
        self, monkeypatch: pytest.MonkeyPatch, loguru_capture: list[str]
    ) -> None:
        request = httpx.Request("GET", "http://voice/v1/voices")
        response = httpx.Response(401, request=request, text="authentication_error")
        monkeypatch.setattr(vas, "_fetch_catalogue", _araise_http_status_error(response))
        called: dict[str, object] = {}
        monkeypatch.setattr(vas.persona_service, "set_voice", lambda **k: called.update(k))
        req = _request(
            config=SimpleNamespace(voice_service_url="http://voice", voice_pick_tier="small"),
            tier_registry=SimpleNamespace(get=lambda _t: _FakeBackend("v1")),
            rls_engine=object(),
            bearer="Bearer t",
        )

        asyncio.run(vas.maybe_assign_voice(req, owner_id="o", persona_id="p", yaml_str=_YAML))

        # Fail-soft is unchanged: create still succeeds, persona stays voiceless.
        assert called == {}
        warnings = [m for m in loguru_capture if "catalogue unavailable" in m]
        assert len(warnings) == 1
        assert "status=401" in warnings[0]
        assert "persona_id=p" in warnings[0]

    def test_upstream_status_surfaces_for_a_different_code_too(
        self, monkeypatch: pytest.MonkeyPatch, loguru_capture: list[str]
    ) -> None:
        request = httpx.Request("GET", "http://voice/v1/voices")
        response = httpx.Response(503, request=request, text="voice_unavailable")
        monkeypatch.setattr(vas, "_fetch_catalogue", _araise_http_status_error(response))
        monkeypatch.setattr(vas.persona_service, "set_voice", lambda **_k: None)
        req = _request(
            config=SimpleNamespace(voice_service_url="http://voice", voice_pick_tier="small"),
            tier_registry=SimpleNamespace(get=lambda _t: _FakeBackend("v1")),
            rls_engine=object(),
            bearer="Bearer t",
        )

        asyncio.run(vas.maybe_assign_voice(req, owner_id="o", persona_id="p", yaml_str=_YAML))

        warnings = [m for m in loguru_capture if "catalogue unavailable" in m]
        assert len(warnings) == 1
        assert "status=503" in warnings[0]
