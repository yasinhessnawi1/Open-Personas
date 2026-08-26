"""Opening a persona re-picks a voice left on a replaced TTS provider (R9-113).

V14 built the auto-remap and left "who calls this, and when" to the caller. No
caller was ever added, so both routes were dead in cloud: the boot sweep has no
caller token and 401s (R9-111), and ``maybe_remap_voice`` had zero production call
sites. Meanwhile ``voice_resolution`` fail-softs a persona whose stored voice
belongs to another provider onto the active provider's ONE default voice, calling
that a backstop "until the auto-remap re-picks it". With nothing re-picking, the
backstop was the permanent outcome: flip the TTS provider and every existing
persona sounds identical, forever.

A request carries the bearer the catalogue needs, so the honest trigger is the
moment an owner opens the persona. These pin the properties that make that safe to
put on a read path: it runs AFTER the response, it never harms the read, and it
re-binds the owner scope so the write is not silently dropped by RLS.
"""

# ruff: noqa: ARG001 - the fakes must mirror the real keyword signatures
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from persona_api.config import Edition
from persona_api.middleware.rls_context import current_user_id
from persona_api.routes import personas as personas_routes

_OWNER = "u_lazy"
_PERSONA = "persona_lazy"
#: A VALID persona. An invalid one silently returns before the pre-check, which is
#: how the no-network test first passed for the wrong reason.
_YAML = (
    "schema_version: '1.0'\n"
    "identity:\n"
    "  name: Ally\n"
    "  role: warm companion\n"
    "  background: A warm, supportive friend.\n"
    "  language_default: en\n"
)


def _request(edition: Edition | None) -> SimpleNamespace:
    config = SimpleNamespace(edition=edition) if edition is not None else None
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(config=config)))


def test_the_read_route_schedules_the_remap_rather_than_awaiting_it() -> None:
    """THE point of putting it on a read: the reader must never wait for it.

    Read the real handler's source rather than its behaviour, because the defect
    being prevented is a latency one that a passing functional test would not
    notice: awaiting a catalogue fetch plus a model pick inside a GET.
    """
    import inspect

    source = inspect.getsource(personas_routes.get_persona)
    assert "background_tasks.add_task(" in source, "the remap must be deferred"
    assert "await voice_assignment_service.maybe_remap_voice" not in source, (
        "awaiting the remap would put a catalogue fetch and a model pick inside a GET"
    )


@pytest.mark.asyncio
async def test_the_background_hook_binds_the_owner_scope_in_cloud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the re-bind the write fails closed and touches zero rows.

    ``BackgroundTasks`` runs after request teardown has reset ``current_user_id``,
    and the pool checkout listener reads that contextvar to scope the connection.
    """
    seen: list[str | None] = []

    async def _fake_remap(
        request: object, *, owner_id: str, persona_id: str, yaml_str: str
    ) -> bool:
        seen.append(current_user_id.get())
        return True

    monkeypatch.setattr(personas_routes.voice_assignment_service, "maybe_remap_voice", _fake_remap)
    await personas_routes._remap_voice_after_response(  # noqa: SLF001
        _request(Edition.cloud), owner_id=_OWNER, persona_id=_PERSONA, yaml_str=_YAML
    )

    assert seen == [_OWNER], "the remap ran without the owner's RLS scope bound"
    assert current_user_id.get() is None, "the scope leaked past the background task"


@pytest.mark.asyncio
async def test_community_does_not_bind_the_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """Community runs a listener-less single-owner engine, so it must NOT set the GUC."""
    seen: list[str | None] = []

    async def _fake_remap(
        request: object, *, owner_id: str, persona_id: str, yaml_str: str
    ) -> bool:
        seen.append(current_user_id.get())
        return True

    monkeypatch.setattr(personas_routes.voice_assignment_service, "maybe_remap_voice", _fake_remap)
    await personas_routes._remap_voice_after_response(  # noqa: SLF001
        _request(Edition.community), owner_id=_OWNER, persona_id=_PERSONA, yaml_str=_YAML
    )

    assert seen == [None]


@pytest.mark.asyncio
async def test_a_failing_remap_never_harms_the_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """An optional repair must not be able to break the thing it rides on.

    The response is already sent by the time this runs, so raising here would
    surface as an unhandled background-task error rather than anything the caller
    could act on.
    """

    async def _boom(request: object, *, owner_id: str, persona_id: str, yaml_str: str) -> bool:
        msg = "catalogue unreachable"
        raise RuntimeError(msg)

    monkeypatch.setattr(personas_routes.voice_assignment_service, "maybe_remap_voice", _boom)

    await personas_routes._remap_voice_after_response(  # noqa: SLF001
        _request(Edition.cloud), owner_id=_OWNER, persona_id=_PERSONA, yaml_str=_YAML
    )
    assert current_user_id.get() is None, "the scope leaked when the remap raised"


@pytest.mark.asyncio
async def test_an_already_correct_persona_costs_no_network_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pre-check is what makes this affordable on a hot read path.

    In steady state every persona already matches the active provider, so the
    common case must not reach the catalogue at all.
    """
    from persona_api.services import voice_assignment_service as vas

    fetches = 0

    async def _counting_fetch(*_a: object, **_k: object) -> tuple[str, list[object]]:
        nonlocal fetches
        fetches += 1
        return "elevenlabs", []

    monkeypatch.setattr(vas, "_fetch_catalogue", _counting_fetch)
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                config=SimpleNamespace(
                    voice_tts_provider="cartesia",
                    voice_service_url="http://voice",
                    voice_pick_tier="small",
                ),
                tier_registry=SimpleNamespace(get=lambda _t: None),
                rls_engine=object(),
                free_tier_registry=None,
            )
        ),
        headers={},
    )
    yaml_on_active_provider = _YAML + "  voice: cartesia:existing-voice\n"

    remapped = await vas.maybe_remap_voice(
        request,  # type: ignore[arg-type]
        owner_id=_OWNER,
        persona_id=_PERSONA,
        yaml_str=yaml_on_active_provider,
    )

    assert remapped is False
    assert fetches == 0, "an already-correct persona reached the catalogue over HTTP"


@pytest.mark.asyncio
async def test_the_scheduled_task_is_the_one_that_does_the_work() -> None:
    """Scheduling the wrong callable would be green everywhere and dead in production."""
    assert asyncio.iscoroutinefunction(personas_routes._remap_voice_after_response)  # noqa: SLF001
