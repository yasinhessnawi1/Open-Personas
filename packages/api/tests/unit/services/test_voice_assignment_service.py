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


# ----- Spec V14 D-V14-13 AUTO-REMAP + D-V14-4 dialect-aware pick ------------


class TestMaybeRemapVoice:
    def _request(self, tier_backend: _FakeBackend, engine: object) -> SimpleNamespace:
        return _request(
            config=SimpleNamespace(voice_service_url="http://voice", voice_pick_tier="small"),
            tier_registry=SimpleNamespace(get=lambda _t: tier_backend),
            rls_engine=engine,
            bearer="Bearer t",
        )

    def test_noop_when_already_on_active_provider(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Active provider cartesia, persona voice cartesia:existing-voice → no remap.
        monkeypatch.setattr(
            vas, "_fetch_catalogue", _aret(("cartesia", [_option("v1", "feminine")]))
        )
        called: dict[str, object] = {}
        monkeypatch.setattr(vas.persona_service, "set_voice", lambda **k: called.update(k))
        result = asyncio.run(
            vas.maybe_remap_voice(
                self._request(_FakeBackend("v1"), object()),
                owner_id="o",
                persona_id="p",
                yaml_str=_YAML_WITH_VOICE,
            )
        )
        assert result is False
        assert called == {}

    def test_remaps_when_provider_mismatches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Active provider elevenlabs, persona voice cartesia → re-pick on elevenlabs.
        monkeypatch.setattr(
            vas,
            "_fetch_catalogue",
            _aret(("elevenlabs", [_option("el1", "feminine"), _option("el2", "masculine")])),
        )
        called: dict[str, object] = {}
        monkeypatch.setattr(vas.persona_service, "set_voice", lambda **k: called.update(k))
        engine = object()
        result = asyncio.run(
            vas.maybe_remap_voice(
                self._request(_FakeBackend("el1"), engine),
                owner_id="o",
                persona_id="p",
                yaml_str=_YAML_WITH_VOICE,
            )
        )
        assert result is True
        assert called == {
            "rls_engine": engine,
            "persona_id": "p",
            "provider": "elevenlabs",
            "voice_id": "el1",
        }

    def test_remaps_voiceless_persona(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A voiceless persona under an active provider is voiced too (covers both).
        monkeypatch.setattr(
            vas, "_fetch_catalogue", _aret(("elevenlabs", [_option("el1", "feminine")]))
        )
        called: dict[str, object] = {}
        monkeypatch.setattr(vas.persona_service, "set_voice", lambda **k: called.update(k))
        result = asyncio.run(
            vas.maybe_remap_voice(
                self._request(_FakeBackend("el1"), object()),
                owner_id="o",
                persona_id="p",
                yaml_str=_YAML,
            )
        )
        assert result is True
        assert called["provider"] == "elevenlabs"

    def test_feature_off_is_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(vas, "_fetch_catalogue", _araise())
        request = _request(
            config=SimpleNamespace(voice_service_url="", voice_pick_tier="small"),
            tier_registry=SimpleNamespace(get=lambda _t: _FakeBackend("v1")),
            rls_engine=object(),
            bearer="Bearer t",
        )
        result = asyncio.run(
            vas.maybe_remap_voice(request, owner_id="o", persona_id="p", yaml_str=_YAML_WITH_VOICE)
        )
        assert result is False

    # ----- Spec V14-T5b — lossless restore-from-memory (review finding I1) ----

    def test_restores_remembered_voice_instead_of_auto_picking(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cartesia -> ElevenLabs -> Cartesia: the persona remembers BOTH voices
        (having previously been remapped through elevenlabs); flipping to
        elevenlabs restores the remembered elevenlabs voice_id verbatim rather
        than asking the model to pick again."""
        monkeypatch.setattr(
            vas,
            "_fetch_catalogue",
            _aret(("elevenlabs", [_option("el-fresh-pick", "feminine")])),
        )
        called: dict[str, object] = {}
        monkeypatch.setattr(vas.persona_service, "set_voice", lambda **k: called.update(k))
        # never-called backend proves the model pick path was NOT taken.
        never_called_backend = SimpleNamespace(chat=_araise())
        yaml_with_memory = (
            _YAML
            + "  voice: cartesia:existing-voice\n"
            + "  voice_by_provider:\n"
            + "    cartesia: existing-voice\n"
            + "    elevenlabs: original-el-voice\n"
        )
        engine = object()
        result = asyncio.run(
            vas.maybe_remap_voice(
                self._request(never_called_backend, engine),  # type: ignore[arg-type]
                owner_id="o",
                persona_id="p",
                yaml_str=yaml_with_memory,
            )
        )
        assert result is True
        assert called == {
            "rls_engine": engine,
            "persona_id": "p",
            "provider": "elevenlabs",
            "voice_id": "original-el-voice",  # restored, NOT "el-fresh-pick"
        }

    def test_no_memory_for_active_provider_falls_through_to_auto_pick(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A persona with memory for a DIFFERENT provider only (no elevenlabs
        entry) still auto-picks fresh under elevenlabs — memory narrows the
        restore, it never blocks the existing auto-pick fallback."""
        monkeypatch.setattr(
            vas,
            "_fetch_catalogue",
            _aret(("elevenlabs", [_option("el1", "feminine")])),
        )
        called: dict[str, object] = {}
        monkeypatch.setattr(vas.persona_service, "set_voice", lambda **k: called.update(k))
        yaml_with_only_cartesia_memory = (
            _YAML
            + "  voice: cartesia:existing-voice\n"
            + "  voice_by_provider:\n"
            + "    cartesia: existing-voice\n"
        )
        result = asyncio.run(
            vas.maybe_remap_voice(
                self._request(_FakeBackend("el1"), object()),
                owner_id="o",
                persona_id="p",
                yaml_str=yaml_with_only_cartesia_memory,
            )
        )
        assert result is True
        assert called["provider"] == "elevenlabs"
        assert called["voice_id"] == "el1"  # auto-picked, not restored (nothing to restore)


class TestDialectAwarePrompt:
    def test_pick_prompt_instructs_dialect_preference(self) -> None:
        """D-V14-4: the auto-pick prompt tells the model to prefer a voice whose
        description notes a dialect/accent suiting the persona's language."""
        msgs = vas._pick_messages(
            _persona(language="ar"),
            [_option("v1", "feminine", name="Layla", description="warm; egyptian accent")],
        )
        system = msgs[0].content
        assert isinstance(system, str)
        lowered = system.lower()
        assert "accent" in lowered
        assert "dialect" in lowered


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


# ----- owner-billing (Spec M3, T5b) ----------------------------------------


class _UsageBackend:
    """A pick backend whose response carries real usage (for the billing path)."""

    provider_name = "openrouter"
    model_name = "m"
    supports_native_tools = False
    supports_vision = False

    async def chat(self, messages: object, **_: object) -> SimpleNamespace:  # noqa: ARG002
        return SimpleNamespace(
            content="v1",
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=1000, cost_usd=0.03),
        )


class _RecordingPolicy:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def capture_up_to_idempotent(self, **kw: object) -> tuple[int, int]:
        self.calls.append(kw)
        return int(kw["amount"]), 0  # type: ignore[call-overload]


class TestVoiceAutopickBilling:
    """T5b: the auto-pick's real model cost is owner-billed, keyed once per persona."""

    def test_owner_billed_with_persona_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            vas,
            "_fetch_catalogue",
            _aret(("cartesia", [_option("v1", "feminine"), _option("v2", "masculine")])),
        )
        monkeypatch.setattr(vas.persona_service, "set_voice", lambda **_k: None)
        policy = _RecordingPolicy()
        request = _request(
            config=SimpleNamespace(voice_service_url="http://voice", voice_pick_tier="small"),
            tier_registry=SimpleNamespace(get=lambda _t: _UsageBackend()),
            rls_engine=object(),
            bearer="Bearer t",
        )
        request.app.state.credits_policy = policy
        asyncio.run(vas.maybe_assign_voice(request, owner_id="o", persona_id="p", yaml_str=_YAML))
        assert len(policy.calls) == 1
        kw = policy.calls[0]
        assert kw["amount"] == 3  # ceil(3.0¢) actual
        assert kw["reason"] == "voice_pick:actual_openrouter"
        assert kw["billing_key"] == "voice_pick:p"
        assert kw["user_id"] == "o"

    def test_unwired_policy_is_a_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            vas, "_fetch_catalogue", _aret(("cartesia", [_option("v1", "feminine")]))
        )
        called: dict[str, object] = {}
        monkeypatch.setattr(vas.persona_service, "set_voice", lambda **k: called.update(k))
        request = _request(  # no credits_policy on state → billing is inert
            config=SimpleNamespace(voice_service_url="http://voice", voice_pick_tier="small"),
            tier_registry=SimpleNamespace(get=lambda _t: _UsageBackend()),
            rls_engine=object(),
            bearer="Bearer t",
        )
        asyncio.run(vas.maybe_assign_voice(request, owner_id="o", persona_id="p", yaml_str=_YAML))
        assert called["voice_id"] == "v1"  # pick still persists; billing just no-ops


# ----- reconcile_voice_assignments (Spec V14 T5a — the D-V14-13 boot trigger) ---


_YAML_ELEVENLABS_VOICE = _YAML + "  voice: elevenlabs:existing-el-voice\n"


class _FakeRow:
    """Mimics a SQLAlchemy ``Row``'s attribute-access shape (``row.yaml`` etc.)."""

    def __init__(self, *, id: str, owner_id: str, yaml: str) -> None:  # noqa: A002
        self.id = id
        self.owner_id = owner_id
        self.yaml = yaml


class _FakeConnCtx:
    def __init__(self, rows: list[_FakeRow]) -> None:
        self._rows = rows

    def __enter__(self) -> _FakeConnCtx:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def execute(self, _stmt: object) -> SimpleNamespace:
        return SimpleNamespace(all=lambda: self._rows)


class _FakeSweepEngine:
    """A stand-in ``sweep_engine`` — records whether ``.begin()`` was ever called."""

    def __init__(self, rows: list[_FakeRow] | None = None) -> None:
        self._rows = rows or []
        self.begin_calls = 0

    def begin(self) -> _FakeConnCtx:
        self.begin_calls += 1
        return _FakeConnCtx(self._rows)


class _RaisingSweepEngine:
    def begin(self) -> _FakeConnCtx:
        raise RuntimeError("persona listing exploded")


def _reconcile_config(
    *, voice_tts_provider: str, voice_service_url: str = "http://voice"
) -> SimpleNamespace:
    return SimpleNamespace(
        voice_service_url=voice_service_url,
        voice_pick_tier="small",
        voice_tts_provider=voice_tts_provider,
    )


class TestReconcileVoiceAssignments:
    def test_mismatched_persona_is_remapped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            vas,
            "_fetch_catalogue",
            _aret(("elevenlabs", [_option("el1", "feminine"), _option("el2", "masculine")])),
        )
        called: dict[str, object] = {}
        monkeypatch.setattr(vas.persona_service, "set_voice", lambda **k: called.update(k))
        sweep = _FakeSweepEngine(
            [_FakeRow(id="p1", owner_id="owner-1", yaml=_YAML_WITH_VOICE)]  # cartesia-voiced
        )
        engine = object()
        counts = asyncio.run(
            vas.reconcile_voice_assignments(
                config=_reconcile_config(voice_tts_provider="elevenlabs"),
                registry=SimpleNamespace(get=lambda _t: _FakeBackend("el1")),
                sweep_engine=sweep,
                rls_engine=engine,
            )
        )
        assert counts == {"scanned": 1, "remapped": 1, "skipped": 0, "failed": 0}
        assert called == {
            "rls_engine": engine,
            "persona_id": "p1",
            "provider": "elevenlabs",
            "voice_id": "el1",
        }

    def test_already_matching_persona_is_skipped_without_a_catalogue_fetch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fetch_calls = 0

        async def _counting_fetch(*_a: object, **_k: object) -> tuple[str, list[object]]:
            nonlocal fetch_calls
            fetch_calls += 1
            return "elevenlabs", []

        monkeypatch.setattr(vas, "_fetch_catalogue", _counting_fetch)
        monkeypatch.setattr(vas.persona_service, "set_voice", _araise())
        sweep = _FakeSweepEngine(
            [_FakeRow(id="p1", owner_id="owner-1", yaml=_YAML_ELEVENLABS_VOICE)]
        )
        counts = asyncio.run(
            vas.reconcile_voice_assignments(
                config=_reconcile_config(voice_tts_provider="elevenlabs"),
                registry=SimpleNamespace(get=lambda _t: _FakeBackend("el1")),
                sweep_engine=sweep,
                rls_engine=object(),
            )
        )
        assert counts == {"scanned": 1, "remapped": 0, "skipped": 1, "failed": 0}
        assert fetch_calls == 0  # the cheap YAML-provider check skipped the network call

    def test_cartesia_active_with_all_cartesia_personas_does_no_catalogue_fetch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Symmetric reconcile (V14-T5b): cartesia is no longer a name-based
        cheap-skip — it still does the (local, cheap) listing, but a boot where
        every persona already matches cartesia pays zero catalogue fetches."""
        fetch_calls = 0

        async def _counting_fetch(*_a: object, **_k: object) -> tuple[str, list[object]]:
            nonlocal fetch_calls
            fetch_calls += 1
            return "cartesia", []

        monkeypatch.setattr(vas, "_fetch_catalogue", _counting_fetch)
        monkeypatch.setattr(vas.persona_service, "set_voice", _araise())
        sweep = _FakeSweepEngine([_FakeRow(id="p1", owner_id="owner-1", yaml=_YAML_WITH_VOICE)])
        counts = asyncio.run(
            vas.reconcile_voice_assignments(
                config=_reconcile_config(voice_tts_provider="cartesia"),
                registry=SimpleNamespace(get=lambda _t: _FakeBackend("v1")),
                sweep_engine=sweep,
                rls_engine=object(),
            )
        )
        assert counts == {"scanned": 1, "remapped": 0, "skipped": 1, "failed": 0}
        assert fetch_calls == 0  # the cheap YAML-provider check skipped the network call
        assert sweep.begin_calls == 1  # the listing itself DOES run (no name-based skip)

    def test_rollback_to_cartesia_restores_the_remembered_voice(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The headline I1 fix: a persona previously remapped to elevenlabs (its
        original cartesia voice remembered along the way) flips BACK to cartesia
        and gets that EXACT original voice back — not a fresh pick, not the
        shared default."""
        monkeypatch.setattr(vas, "_fetch_catalogue", _aret(("cartesia", [])))
        called: dict[str, object] = {}
        monkeypatch.setattr(vas.persona_service, "set_voice", lambda **k: called.update(k))
        yaml_now_on_elevenlabs = (
            _YAML
            + "  voice: elevenlabs:remapped-voice\n"
            + "  voice_by_provider:\n"
            + "    cartesia: original-cartesia-voice\n"
            + "    elevenlabs: remapped-voice\n"
        )
        sweep = _FakeSweepEngine(
            [_FakeRow(id="p1", owner_id="owner-1", yaml=yaml_now_on_elevenlabs)]
        )
        engine = object()
        counts = asyncio.run(
            vas.reconcile_voice_assignments(
                config=_reconcile_config(voice_tts_provider="cartesia"),
                registry=SimpleNamespace(get=lambda _t: _FakeBackend("should-not-be-picked")),
                sweep_engine=sweep,
                rls_engine=engine,
            )
        )
        assert counts == {"scanned": 1, "remapped": 1, "skipped": 0, "failed": 0}
        assert called == {
            "rls_engine": engine,
            "persona_id": "p1",
            "provider": "cartesia",
            "voice_id": "original-cartesia-voice",  # restored verbatim, not re-picked
        }

    def test_persona_with_no_provider_memory_auto_picks_and_is_scanned_bidirectionally(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A persona created entirely under elevenlabs (no cartesia original) that
        flips to cartesia has no memory to restore, so it auto-picks a FRESH
        cartesia voice (not a shared default) — proving the reconcile loop is
        genuinely bidirectional, not just cartesia-tolerant."""
        monkeypatch.setattr(
            vas, "_fetch_catalogue", _aret(("cartesia", [_option("c1", "feminine")]))
        )
        called: dict[str, object] = {}
        monkeypatch.setattr(vas.persona_service, "set_voice", lambda **k: called.update(k))
        sweep = _FakeSweepEngine(
            [_FakeRow(id="p1", owner_id="owner-1", yaml=_YAML_ELEVENLABS_VOICE)]
        )
        counts = asyncio.run(
            vas.reconcile_voice_assignments(
                config=_reconcile_config(voice_tts_provider="cartesia"),
                registry=SimpleNamespace(get=lambda _t: _FakeBackend("c1")),
                sweep_engine=sweep,
                rls_engine=object(),
            )
        )
        assert counts == {"scanned": 1, "remapped": 1, "skipped": 0, "failed": 0}
        assert called["provider"] == "cartesia"
        assert called["voice_id"] == "c1"

    def test_feature_unconfigured_is_a_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(vas, "_fetch_catalogue", _araise())
        sweep = _RaisingSweepEngine()
        counts = asyncio.run(
            vas.reconcile_voice_assignments(
                config=_reconcile_config(voice_tts_provider="elevenlabs", voice_service_url=""),
                registry=SimpleNamespace(get=lambda _t: _FakeBackend("v1")),
                sweep_engine=sweep,
                rls_engine=object(),
            )
        )
        assert counts == {"scanned": 0, "remapped": 0, "skipped": 0, "failed": 0}

    def test_missing_composition_pieces_is_a_noop(self) -> None:
        counts = asyncio.run(
            vas.reconcile_voice_assignments(
                config=None, registry=None, sweep_engine=None, rls_engine=None
            )
        )
        assert counts == {"scanned": 0, "remapped": 0, "skipped": 0, "failed": 0}

    def test_listing_failure_logs_once_and_never_crashes(
        self, monkeypatch: pytest.MonkeyPatch, loguru_capture: list[str]
    ) -> None:
        monkeypatch.setattr(vas, "_fetch_catalogue", _araise())
        counts = asyncio.run(
            vas.reconcile_voice_assignments(
                config=_reconcile_config(voice_tts_provider="elevenlabs"),
                registry=SimpleNamespace(get=lambda _t: _FakeBackend("v1")),
                sweep_engine=_RaisingSweepEngine(),
                rls_engine=object(),
            )
        )
        assert counts == {"scanned": 0, "remapped": 0, "skipped": 0, "failed": 0}
        warnings = [m for m in loguru_capture if "persona listing failed" in m]
        assert len(warnings) == 1  # exactly once — never per-persona spam on a listing failure

    def test_corrupt_persona_row_is_counted_failed_not_crashing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(vas, "_fetch_catalogue", _araise())  # never reached
        bad_row = _FakeRow(id="p1", owner_id="owner-1", yaml="not: [valid persona")
        sweep = _FakeSweepEngine([bad_row])
        counts = asyncio.run(
            vas.reconcile_voice_assignments(
                config=_reconcile_config(voice_tts_provider="elevenlabs"),
                registry=SimpleNamespace(get=lambda _t: _FakeBackend("v1")),
                sweep_engine=sweep,
                rls_engine=object(),
            )
        )
        assert counts == {"scanned": 1, "remapped": 0, "skipped": 0, "failed": 1}
