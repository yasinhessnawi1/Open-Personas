"""The once-and-cached skill summariser (R9-165) driven end to end.

The owner's ruling: a skill is static content, so it is summarised ONCE, cached keyed on the
body's content hash, and served by ``inject`` as a dictionary lookup; never per turn. These
tests run the real chain (``ensure_skill_summaries`` → ``SkillSummaryCache`` →
``SkillInjector.inject``) with a spying backend, so "never inside inject" is asserted on
the backend's call count, not on a comment.
"""

# ruff: noqa: ANN401, ARG002

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path  # noqa: TC003 — runtime use in fixtures
from typing import Any

import pytest
from loguru import logger as _loguru_logger
from persona.backends.errors import ProviderError
from persona.backends.types import ChatResponse, TokenUsage
from persona.schema.skills import SkillProvenance, SkillSpec, SkillTrust
from persona.skills._tokens import count_tokens
from persona.skills.injector import MARKER, SkillInjector, content_hash_of
from persona.skills.sources.ingest import ingest_external_skill
from persona.skills.summary import (
    SKILL_SUMMARY_CACHE_FILENAME,
    SkillSummary,
    SkillSummaryCache,
    ensure_skill_summaries,
    resolve_skill_summary_cache_path,
)

_BIG = "Step one: evaluate the source. Step two: record the claim. " * 400  # > 2000 tokens


class _SpyBackend:
    """A ChatBackend double: fixed replies in order, every call recorded."""

    provider_name = "fake"
    model_name = "tiny"
    supports_native_tools = False
    supports_vision = False

    def __init__(self, *replies: str, explode: bool = False) -> None:
        self._replies = list(replies) or ["Condensed skill."]
        self._explode = explode
        self.calls: list[list[Any]] = []

    async def chat(self, messages: list[Any], **_: object) -> ChatResponse:
        self.calls.append(messages)
        if self._explode:
            raise ProviderError("provider 503")
        reply = self._replies[min(len(self.calls), len(self._replies)) - 1]
        return ChatResponse(
            content=reply,
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            model=self.model_name,
            provider=self.provider_name,
            latency_ms=1.0,
        )

    def chat_stream(self, messages: list[Any], **_: object) -> Any:
        raise NotImplementedError


def _spec(tmp_path: Path, name: str, content: str, **extra: object) -> SkillSpec:
    return SkillSpec(
        name=name,
        description="d",
        path=tmp_path,
        content=content,
        content_token_count=count_tokens(content),
        provenance=SkillProvenance(source="builtin", content_hash=content_hash_of_text(content)),
        **extra,  # type: ignore[arg-type]
    )


def content_hash_of_text(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _warnings() -> tuple[list[str], int]:
    captured: list[str] = []
    sink = _loguru_logger.add(lambda m: captured.append(str(m)), level="WARNING")
    return captured, sink


# --- the chain the brief names ----------------------------------------------------------


@pytest.mark.asyncio
async def test_over_budget_skill_with_cached_summary_is_injected_as_the_summary(
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path, "web_research", _BIG)
    cache = SkillSummaryCache()
    backend = _SpyBackend("# Web research\n1. Evaluate the source.\n2. Record the claim.")

    report = await ensure_skill_summaries([spec], cache=cache, backend=backend)

    assert report.summarised == ("web_research",)
    out = await SkillInjector(summaries=cache).inject(spec)
    assert out == "# Web research\n1. Evaluate the source.\n2. Record the claim."
    assert MARKER not in out


@pytest.mark.asyncio
async def test_without_a_summary_it_is_truncated_and_the_warning_fires(tmp_path: Path) -> None:
    spec = _spec(tmp_path, "web_research", _BIG)
    captured, sink = _warnings()
    try:
        out = await SkillInjector(summaries=SkillSummaryCache()).inject(spec)
    finally:
        _loguru_logger.remove(sink)
    assert out.endswith(MARKER)
    assert count_tokens(out) <= SkillInjector.TOKEN_BUDGET
    assert any("web_research" in m and "truncated" in m.lower() for m in captured)


@pytest.mark.asyncio
async def test_a_changed_body_does_not_reuse_a_stale_summary(tmp_path: Path) -> None:
    """Invalidation by construction: the key is the body's hash, so an edit misses."""
    original = _spec(tmp_path, "web_research", _BIG)
    cache = SkillSummaryCache()
    backend = _SpyBackend("Summary of the ORIGINAL body.")
    await ensure_skill_summaries([original], cache=cache, backend=backend)

    edited_body = _BIG + "\n\n## New rubric\nA line the summary knows nothing about."
    edited = original.model_copy(
        update={"content": edited_body, "content_token_count": count_tokens(edited_body)}
    )
    assert content_hash_of(edited) != content_hash_of(original)

    out = await SkillInjector(summaries=cache).inject(edited)
    assert out != "Summary of the ORIGINAL body."
    assert out.endswith(MARKER)
    # Re-running the producer summarises the new body; the old record is untouched.
    report = await ensure_skill_summaries([edited], cache=cache, backend=backend)
    assert report.summarised == ("web_research",)
    assert len(cache) == 2


@pytest.mark.asyncio
async def test_a_summary_is_never_produced_inside_inject(tmp_path: Path) -> None:
    """The backend is called by the producer only; N injections (hits AND misses) add zero."""
    cached_spec = _spec(tmp_path, "cached", _BIG)
    uncached_spec = _spec(tmp_path, "uncached", _BIG + " tail that makes a new hash.")
    cache = SkillSummaryCache()
    backend = _SpyBackend("Short.")
    await ensure_skill_summaries([cached_spec], cache=cache, backend=backend)
    assert len(backend.calls) == 1

    injector = SkillInjector(summaries=cache)
    for _ in range(3):
        assert await injector.inject(cached_spec) == "Short."
        assert (await injector.inject(uncached_spec)).endswith(MARKER)
    assert len(backend.calls) == 1, "inject must never call the model"


@pytest.mark.asyncio
async def test_ingest_of_an_over_budget_skill_records_a_summary(tmp_path: Path) -> None:
    """The ingest chain: SKILL.md on disk → ingest_external_skill → summary cached by its hash."""
    skill_dir = tmp_path / "huge_skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: huge_skill\ndescription: An external skill far over budget.\n---\n" + _BIG,
        encoding="utf-8",
    )
    spec = ingest_external_skill(
        skill_dir / "SKILL.md", trust=SkillTrust.THIRD_PARTY, source="github:o/r"
    )
    assert spec is not None
    assert spec.content_token_count > SkillInjector.TOKEN_BUDGET

    cache = SkillSummaryCache(tmp_path / SKILL_SUMMARY_CACHE_FILENAME)
    report = await ensure_skill_summaries(
        [spec], cache=cache, backend=_SpyBackend("The condensed external skill.")
    )
    assert report.summarised == ("huge_skill",)
    # Keyed on the ingested body's hash, which is exactly the provenance hash for an
    # unaltered ingest (S1-D-5), and persisted beside the mirror.
    assert spec.provenance is not None
    assert cache.get(spec.provenance.content_hash, budget=2000) == "The condensed external skill."
    on_disk = json.loads((tmp_path / SKILL_SUMMARY_CACHE_FILENAME).read_text())
    assert spec.provenance.content_hash in on_disk["summaries"]
    assert await SkillInjector(summaries=cache).inject(spec) == "The condensed external skill."


# --- producer behaviour -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_warm_cache_costs_no_model_call(tmp_path: Path) -> None:
    spec = _spec(tmp_path, "web_research", _BIG)
    cache = SkillSummaryCache()
    backend = _SpyBackend("Short.")
    await ensure_skill_summaries([spec], cache=cache, backend=backend)
    report = await ensure_skill_summaries([spec], cache=cache, backend=backend)
    assert report.cached == ("web_research",)
    assert report.summarised == ()
    assert len(backend.calls) == 1


@pytest.mark.asyncio
async def test_under_budget_skills_are_left_alone(tmp_path: Path) -> None:
    small = _spec(tmp_path, "small", "Fits easily.")
    backend = _SpyBackend()
    report = await ensure_skill_summaries([small], cache=SkillSummaryCache(), backend=backend)
    assert report == type(report)()
    assert backend.calls == []


@pytest.mark.asyncio
async def test_no_backend_names_the_skill_with_its_numbers_and_leaves_truncation(
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path, "web_research", _BIG)
    cache = SkillSummaryCache()
    captured, sink = _warnings()
    try:
        report = await ensure_skill_summaries([spec], cache=cache, backend=None)
    finally:
        _loguru_logger.remove(sink)
    assert report.unavailable == ("web_research",)
    assert len(cache) == 0
    said = " ".join(captured)
    assert "web_research" in said
    assert str(spec.content_token_count) in said
    assert "2000" in said
    assert str(spec.content_token_count - 2000) in said


@pytest.mark.asyncio
async def test_summary_that_never_fits_is_not_cached(tmp_path: Path) -> None:
    spec = _spec(tmp_path, "web_research", _BIG)
    cache = SkillSummaryCache()
    backend = _SpyBackend("still too long " * 1500)
    captured, sink = _warnings()
    try:
        report = await ensure_skill_summaries([spec], cache=cache, backend=backend)
    finally:
        _loguru_logger.remove(sink)
    assert report.failed == ("web_research",)
    assert len(backend.calls) == 2, "one retry at a tighter word cap, then give up"
    assert len(cache) == 0
    assert any("web_research" in m for m in captured)


@pytest.mark.asyncio
async def test_second_attempt_that_fits_is_cached(tmp_path: Path) -> None:
    spec = _spec(tmp_path, "web_research", _BIG)
    cache = SkillSummaryCache()
    backend = _SpyBackend("still too long " * 1500, "Now it fits.")
    report = await ensure_skill_summaries([spec], cache=cache, backend=backend)
    assert report.summarised == ("web_research",)
    assert cache.get(content_hash_of(spec), budget=2000) == "Now it fits."
    # The retry asked for fewer words than the first attempt.
    first, second = (m[0].content for m in backend.calls)
    assert first != second


@pytest.mark.asyncio
async def test_provider_failure_is_fail_soft(tmp_path: Path) -> None:
    spec = _spec(tmp_path, "web_research", _BIG)
    cache = SkillSummaryCache()
    report = await ensure_skill_summaries([spec], cache=cache, backend=_SpyBackend(explode=True))
    assert report.failed == ("web_research",)
    assert len(cache) == 0


@pytest.mark.asyncio
async def test_prompt_keeps_procedure_over_prose_and_targets_the_budget(tmp_path: Path) -> None:
    spec = _spec(tmp_path, "web_research", _BIG, token_budget=1000)
    backend = _SpyBackend("Short.")
    await ensure_skill_summaries([spec], cache=SkillSummaryCache(), backend=backend)
    system, user = backend.calls[0]
    assert system.role == "system"
    assert "heading" in system.content
    assert "rubric" in system.content
    assert user.content == _BIG
    # Word cap derived from the per-skill budget minus the marker (995 tokens * 0.7).
    assert "696 words" in system.content


# --- the cache itself -------------------------------------------------------------------


def _record(text: str, *, body: str = _BIG) -> SkillSummary:
    return SkillSummary(
        content_hash=content_hash_of_text(body),
        summary=text,
        summary_token_count=count_tokens(text),
        budget=2000,
        model="fake/tiny",
        created_at=datetime.now(UTC),
    )


def test_cache_persists_atomically_and_another_instance_sees_it(tmp_path: Path) -> None:
    path = tmp_path / "nested" / SKILL_SUMMARY_CACHE_FILENAME
    writer = SkillSummaryCache(path)
    writer.put(_record("From the writer."))
    assert path.exists()
    assert not [p for p in path.parent.iterdir() if p.name.startswith(".skill_summaries.")]
    reader = SkillSummaryCache(path)
    assert reader.get(content_hash_of_text(_BIG), budget=2000) == "From the writer."


def test_reader_picks_up_a_later_write_by_mtime(tmp_path: Path) -> None:
    path = tmp_path / SKILL_SUMMARY_CACHE_FILENAME
    reader = SkillSummaryCache(path)
    assert reader.get(content_hash_of_text(_BIG), budget=2000) is None
    writer = SkillSummaryCache(path)
    writer.put(_record("Written later."))
    # Force a visibly newer mtime even on coarse filesystems.
    later = path.stat().st_mtime + 2
    os.utime(path, (later, later))
    assert reader.get(content_hash_of_text(_BIG), budget=2000) == "Written later."


def test_get_honours_the_budget_it_is_asked_for() -> None:
    cache = SkillSummaryCache()
    cache.put(_record("ten tokens or so of summary text here."))
    assert cache.get(content_hash_of_text(_BIG), budget=2000) is not None
    assert cache.get(content_hash_of_text(_BIG), budget=3) is None


def test_corrupt_cache_file_starts_empty_and_does_not_raise(tmp_path: Path) -> None:
    path = tmp_path / SKILL_SUMMARY_CACHE_FILENAME
    path.write_text("{not json", encoding="utf-8")
    cache = SkillSummaryCache(path)
    assert len(cache) == 0
    cache.put(_record("Recovered."))
    assert json.loads(path.read_text())["version"] == 1


def test_cache_path_precedence(tmp_path: Path) -> None:
    override = tmp_path / "x.json"
    mirror = tmp_path / "vol" / "skill_mirror.json"
    data = tmp_path / ".chroma"
    assert resolve_skill_summary_cache_path(override, mirror_path=mirror, data_root=data) == (
        override
    )
    assert resolve_skill_summary_cache_path(None, mirror_path=mirror, data_root=data) == (
        tmp_path / "vol" / SKILL_SUMMARY_CACHE_FILENAME
    )
    assert resolve_skill_summary_cache_path(None, mirror_path=None, data_root=data) == (
        data / SKILL_SUMMARY_CACHE_FILENAME
    )
