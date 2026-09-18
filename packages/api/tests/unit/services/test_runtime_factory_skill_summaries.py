"""R9-165: the runtime factory wires the once-and-cached skill summariser, reachably.

Two production ``SkillInjector`` constructions (chat loop + agentic loop) used to pass no
summariser, so every over-budget skill was character-cut; the built-in ``web_research`` lost
its whole source-evaluation rubric on every injection. These tests drive the real chain the
api composes: ``warm_skill_summaries`` (boot, background tier, ownerless) → the cache file
beside the mirror → the injector the loops get from ``_build_skill_injector`` → the summary
is what ``inject`` returns for the scanned built-in, with the backend untouched by injection.
"""

# ruff: noqa: ANN401

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any

import pytest
from persona.backends.errors import TierNotConfiguredError
from persona.backends.types import ChatResponse, TokenUsage
from persona.config import PersonaCoreConfig
from persona.schema.persona import Persona, PersonaIdentity
from persona.skills.injector import MARKER, SkillInjector
from persona.skills.summary import SkillSummaryCache
from persona_api.services import runtime_factory as factory_module
from persona_api.services.runtime_factory import RuntimeFactory, tier_for

if TYPE_CHECKING:
    from pathlib import Path


class _SpyBackend:
    provider_name = "fake"
    model_name = "tiny"
    supports_native_tools = False
    supports_vision = False

    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.calls: list[Any] = []

    async def chat(self, messages: list[Any], **_: object) -> ChatResponse:
        self.calls.append(messages)
        return ChatResponse(
            content=self._reply,
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            model=self.model_name,
            provider=self.provider_name,
            latency_ms=1.0,
        )

    def chat_stream(self, messages: list[Any], **_: object) -> Any:
        raise NotImplementedError


class _Registry:
    """A TierRegistry double: records the tier asked for; returns a backend or raises."""

    def __init__(self, backend: _SpyBackend | None, *, raise_: Exception | None = None) -> None:
        self._backend = backend
        self._raise = raise_
        self.asked: list[str] = []

    def get(self, tier: str) -> _SpyBackend | None:
        self.asked.append(tier)
        if self._raise is not None:
            raise self._raise
        return self._backend


def _factory(tmp_path: Path, *, registry: object, enabled: bool = True) -> RuntimeFactory:
    core = PersonaCoreConfig(
        skill_summaries_enabled=enabled,
        skill_mirror_path=tmp_path / "mirror" / "skill_mirror.json",
        skill_summary_cache_path=None,  # resolves to beside the mirror
    )
    return RuntimeFactory(
        rls_engine=object(),  # type: ignore[arg-type]
        embedder=None,  # type: ignore[arg-type]
        tier_registry=registry,  # type: ignore[arg-type]
        turn_log_writer=None,  # type: ignore[arg-type]
        audit_root=tmp_path,
        core_config=core,
    )


def _persona(skills: list[str]) -> Persona:
    return Persona(
        persona_id="p_skills",
        identity=PersonaIdentity(name="Researcher", role="Analyst", background="Reads sources."),
        skills=skills,
    )


async def _scanned_web_research(factory: RuntimeFactory) -> Any:
    _scanner, scanned = factory._scan_skills(_persona(["web_research"]))  # noqa: SLF001
    return next(s for s in scanned if s.name == "web_research")  # type: ignore[attr-defined]


def test_both_loops_build_their_injector_through_the_one_wired_construction() -> None:
    """Neither loop constructs a bare ``SkillInjector()`` any more; both go via the seam."""
    source = inspect.getsource(factory_module)
    assert "skill_injector=SkillInjector()" not in source
    assert source.count("skill_injector=self._build_skill_injector()") == 2


def test_injector_reads_the_cache_beside_the_mirror(tmp_path: Path) -> None:
    factory = _factory(tmp_path, registry=None)
    injector = factory._build_skill_injector()  # noqa: SLF001
    cache = factory.skill_summaries
    assert isinstance(cache, SkillSummaryCache)
    assert injector._summaries is cache  # noqa: SLF001
    assert cache.path == tmp_path / "mirror" / "skill_summaries.json"


def test_disabled_flag_is_the_pre_fix_injector(tmp_path: Path) -> None:
    factory = _factory(tmp_path, registry=None, enabled=False)
    assert factory.skill_summaries is None
    assert factory._build_skill_injector()._summaries is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_boot_warm_summarises_web_research_and_the_loops_injector_serves_it(
    tmp_path: Path,
) -> None:
    backend = _SpyBackend("## Web research, condensed\n1. Evaluate the source.")
    registry = _Registry(backend)
    factory = _factory(tmp_path, registry=registry)

    report = await factory.warm_skill_summaries()

    assert report is not None
    assert "web_research" in report.summarised
    assert registry.asked == [tier_for("background")], "the small surface, resolved once"
    warm_calls = len(backend.calls)
    assert warm_calls == len(report.summarised)
    assert (tmp_path / "mirror" / "skill_summaries.json").exists()

    spec = await _scanned_web_research(factory)
    assert spec.content_token_count > SkillInjector.TOKEN_BUDGET
    out = await factory._build_skill_injector().inject(spec)  # noqa: SLF001
    assert out == "## Web research, condensed\n1. Evaluate the source."
    assert len(backend.calls) == warm_calls, "injection never calls the model"

    # The next boot finds a warm cache: nothing to do, no model call.
    again = await factory.warm_skill_summaries()
    assert again is not None
    assert "web_research" in again.cached
    assert again.summarised == ()
    assert len(backend.calls) == warm_calls


@pytest.mark.asyncio
async def test_no_registry_leaves_truncation_with_its_warning(tmp_path: Path) -> None:
    factory = _factory(tmp_path, registry=None)
    report = await factory.warm_skill_summaries()
    assert report is not None
    assert "web_research" in report.unavailable
    assert report.summarised == ()
    spec = await _scanned_web_research(factory)
    out = await factory._build_skill_injector().inject(spec)  # noqa: SLF001
    assert out.endswith(MARKER)


@pytest.mark.asyncio
async def test_unconfigured_background_tier_is_fail_soft(tmp_path: Path) -> None:
    registry = _Registry(None, raise_=TierNotConfiguredError("no small tier"))
    factory = _factory(tmp_path, registry=registry)
    report = await factory.warm_skill_summaries()
    assert report is not None
    assert "web_research" in report.unavailable
    assert registry.asked == [tier_for("background")]


@pytest.mark.asyncio
async def test_disabled_warm_is_a_no_op(tmp_path: Path) -> None:
    backend = _SpyBackend("unused")
    factory = _factory(tmp_path, registry=_Registry(backend), enabled=False)
    assert await factory.warm_skill_summaries() is None
    assert backend.calls == []
