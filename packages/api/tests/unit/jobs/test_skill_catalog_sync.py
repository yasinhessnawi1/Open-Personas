"""Unit tests for the skill-catalog auto-sync task (Spec S2, C1).

No DB / no network: the leader gate is a fake (the ``leader_factory`` seam) and the
clone+sync is injected (the ``sync_runner`` seam), so we exercise leader-gating + fail-soft
without Postgres or a git clone. Mirrors N2's ``test_catalog_sync`` shape — the skill sync
reuses the same leader-gated substrate with a **distinct** advisory-lock key (S2-D-X-leader-key)
so skill-sync leadership is orthogonal to MCP-catalog leadership.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from persona.skills.skill_mirror_reconcile import SkillMirrorSyncResult
from persona_api.jobs.catalog_sync import CATALOG_SYNC_LEADER_LOCK_KEY
from persona_api.jobs.skill_catalog_sync import (
    SKILL_CATALOG_SYNC_LEADER_LOCK_KEY,
    SkillCatalogSyncTask,
)

if TYPE_CHECKING:
    from pathlib import Path


class _FakeLeader:
    def __init__(self, *, wins: bool) -> None:
        self._wins = wins
        self.resigned = False

    def try_become_leader(self) -> bool:
        return self._wins

    def resign(self) -> None:
        self.resigned = True


def _task(*, wins: bool, runner: object, leaders: list[_FakeLeader]) -> SkillCatalogSyncTask:
    def _factory() -> _FakeLeader:
        leader = _FakeLeader(wins=wins)
        leaders.append(leader)
        return leader

    return SkillCatalogSyncTask(
        dispatch_engine=MagicMock(),
        sync_runner=runner,  # type: ignore[arg-type]
        leader_factory=_factory,
    )


def test_distinct_lock_key_from_mcp_catalog_sync() -> None:
    """The skill-sync leader key differs from the MCP catalog-sync key (orthogonal)."""
    assert SKILL_CATALOG_SYNC_LEADER_LOCK_KEY != CATALOG_SYNC_LEADER_LOCK_KEY


def test_no_op_when_not_leader() -> None:
    def _boom() -> SkillMirrorSyncResult:
        raise AssertionError("sync must NOT run when not leader")

    leaders: list[_FakeLeader] = []
    assert _task(wins=False, runner=_boom, leaders=leaders).run_once() is None
    # Not-leader still resigns cleanly (no held lock leaked).
    assert leaders[0].resigned is True


def test_runs_and_returns_result_when_leader() -> None:
    result = SkillMirrorSyncResult(added=("a",), updated=(), removed=(), total=1)
    leaders: list[_FakeLeader] = []
    out = _task(wins=True, runner=lambda: result, leaders=leaders).run_once()
    assert out is result
    assert leaders[0].resigned is True


def test_failsoft_on_sync_error_returns_none_and_resigns() -> None:
    def _explode() -> SkillMirrorSyncResult:
        raise RuntimeError("clone failed")

    leaders: list[_FakeLeader] = []
    # Fail-soft: the last-good mirror is preserved (reconcile raises before write); the
    # task catches, logs, returns None, and ALWAYS resigns the leader lock.
    assert _task(wins=True, runner=_explode, leaders=leaders).run_once() is None
    assert leaders[0].resigned is True


# --------------------------------------------------------------------------- #
# build_skill_catalog_sync — the opt-out + the read-only bundled snapshot     #
# --------------------------------------------------------------------------- #


def test_build_skill_catalog_sync_disabled_returns_none() -> None:
    from persona_api.config import APIConfig
    from persona_api.jobs.skill_catalog_sync import build_skill_catalog_sync

    config = APIConfig(skill_catalog_sync_enabled=False)
    assert build_skill_catalog_sync(config, dispatch_engine=MagicMock()) is None


def test_build_skill_catalog_sync_enabled_builds_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from persona_api.config import APIConfig
    from persona_api.jobs.skill_catalog_sync import build_skill_catalog_sync

    monkeypatch.setenv("PERSONA_SKILL_MIRROR_PATH", str(tmp_path / "skill_mirror.json"))
    config = APIConfig(skill_catalog_sync_enabled=True)
    task = build_skill_catalog_sync(config, dispatch_engine=MagicMock())
    assert isinstance(task, SkillCatalogSyncTask)


def test_build_skill_catalog_sync_without_mirror_path_skips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R9-011: no PERSONA_SKILL_MIRROR_PATH → the sync is skipped (warn), NOT pointed at
    the bundled package-data snapshot (a committed file the sync must never rewrite)."""
    from persona_api.config import APIConfig
    from persona_api.jobs.skill_catalog_sync import build_skill_catalog_sync

    monkeypatch.delenv("PERSONA_SKILL_MIRROR_PATH", raising=False)
    config = APIConfig(skill_catalog_sync_enabled=True)
    assert build_skill_catalog_sync(config, dispatch_engine=MagicMock()) is None


# --------------------------------------------------------------------------- #
# R9-040 — PERSONA_SKILL_GITHUB_REPOS threads config -> the sync task         #
# --------------------------------------------------------------------------- #


def test_build_skill_catalog_sync_threads_github_repos_from_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from persona.skills.sources.github import GithubRepoSpec
    from persona_api.config import APIConfig
    from persona_api.jobs.skill_catalog_sync import build_skill_catalog_sync

    monkeypatch.setenv("PERSONA_SKILL_MIRROR_PATH", str(tmp_path / "skill_mirror.json"))
    config = APIConfig(
        skill_catalog_sync_enabled=True,
        skill_github_repos="acme/skills@main,other/repo2",
    )
    task = build_skill_catalog_sync(config, dispatch_engine=MagicMock())
    assert isinstance(task, SkillCatalogSyncTask)
    assert task._github_repos == [  # noqa: SLF001 — proving the wiring, not just "it builds"
        GithubRepoSpec(owner="acme", repo="skills", ref="main"),
        GithubRepoSpec(owner="other", repo="repo2", ref=None),
    ]


def test_build_skill_catalog_sync_defaults_to_no_github_repos(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from persona_api.config import APIConfig
    from persona_api.jobs.skill_catalog_sync import build_skill_catalog_sync

    monkeypatch.setenv("PERSONA_SKILL_MIRROR_PATH", str(tmp_path / "skill_mirror.json"))
    config = APIConfig(skill_catalog_sync_enabled=True)
    task = build_skill_catalog_sync(config, dispatch_engine=MagicMock())
    assert isinstance(task, SkillCatalogSyncTask)
    assert task._github_repos == []  # noqa: SLF001


def test_skill_github_repos_parsed_good_bad_and_empty() -> None:
    from persona.skills.sources.github import GithubRepoSpec
    from persona_api.config import APIConfig

    assert APIConfig(skill_github_repos="").skill_github_repos_parsed == []
    assert APIConfig(skill_github_repos="acme/skills").skill_github_repos_parsed == [
        GithubRepoSpec(owner="acme", repo="skills", ref=None)
    ]
    # A malformed entry warns-and-skips; config load itself never raises (R9-040).
    cfg = APIConfig(skill_github_repos="acme/skills,not-valid,other/repo2@v1")
    assert cfg.skill_github_repos_parsed == [
        GithubRepoSpec(owner="acme", repo="skills", ref=None),
        GithubRepoSpec(owner="other", repo="repo2", ref="v1"),
    ]


def test_skill_github_repos_env_var_drives_the_field(monkeypatch: pytest.MonkeyPatch) -> None:
    from persona_api.config import APIConfig

    monkeypatch.setenv("PERSONA_SKILL_GITHUB_REPOS", "acme/skills")
    assert APIConfig().skill_github_repos == "acme/skills"


# --- R9-165: the post-sync summariser ---------------------------------------------------


class _SpyBackend:
    provider_name = "fake"
    model_name = "tiny"
    supports_native_tools = False
    supports_vision = False

    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.calls: list[object] = []

    async def chat(self, messages: list[object], **_: object) -> object:
        from persona.backends.types import ChatResponse, TokenUsage

        self.calls.append(messages)
        return ChatResponse(
            content=self._reply,
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            model=self.model_name,
            provider=self.provider_name,
            latency_ms=1.0,
        )

    def chat_stream(self, messages: list[object], **_: object) -> object:
        raise NotImplementedError


def _over_budget_mirror(tmp_path: Path) -> tuple[Path, str]:
    """Write a mirror snapshot holding one over-budget external skill; return (path, hash)."""
    import hashlib

    from persona.schema.skills import SkillProvenance, SkillSpec, SkillTrust
    from persona.skills._tokens import count_tokens
    from persona.skills.skill_mirror import write_skill_mirror_atomic

    body = "Step: evaluate the source. Step: record the claim. " * 400
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    spec = SkillSpec(
        name="huge",
        description="An external skill far over budget.",
        path=tmp_path / "huge",
        content=body,
        content_token_count=count_tokens(body),
        trust=SkillTrust.COMMUNITY,
        provenance=SkillProvenance(source="openclaw", content_hash=digest),
    )
    mirror = tmp_path / "skill_mirror.json"
    write_skill_mirror_atomic([spec], mirror)
    return mirror, digest


def _synced_result() -> SkillMirrorSyncResult:
    return SkillMirrorSyncResult(added=("huge",), updated=(), removed=(), total=1)


@pytest.mark.asyncio
async def test_leader_sync_then_summarise_caches_the_mirrored_skill_once(tmp_path: Path) -> None:
    """Ingest chain: mirror written by the sync → summarise_synced → cache by hash → injected."""
    from persona.skills.injector import SkillInjector
    from persona.skills.skill_mirror import load_skill_mirror
    from persona.skills.summary import SkillSummaryCache

    mirror, digest = _over_budget_mirror(tmp_path)
    backend = _SpyBackend("The condensed mirrored skill.")
    cache = SkillSummaryCache(tmp_path / "skill_summaries.json")
    leaders: list[_FakeLeader] = []

    def _factory() -> _FakeLeader:
        leader = _FakeLeader(wins=True)
        leaders.append(leader)
        return leader

    task = SkillCatalogSyncTask(
        dispatch_engine=MagicMock(),
        mirror_path=mirror,
        sync_runner=_synced_result,
        leader_factory=_factory,
        summary_cache=cache,
        summary_backend=backend,  # type: ignore[arg-type]
    )
    assert task.run_once() is not None

    report = await task.summarise_synced()
    assert report is not None
    assert report.summarised == ("huge",)
    assert cache.get(digest, budget=2000) == "The condensed mirrored skill."
    assert (tmp_path / "skill_summaries.json").exists(), "persisted beside the mirror"

    (loaded,) = load_skill_mirror(mirror)
    assert await SkillInjector(summaries=cache).inject(loaded) == "The condensed mirrored skill."
    assert len(backend.calls) == 1

    # A second sync with an unchanged body costs no model call.
    again = await task.summarise_synced()
    assert again is not None
    assert again.cached == ("huge",)
    assert len(backend.calls) == 1


@pytest.mark.asyncio
async def test_summarise_synced_without_a_cache_is_a_no_op(tmp_path: Path) -> None:
    mirror, _ = _over_budget_mirror(tmp_path)
    backend = _SpyBackend("unused")
    task = SkillCatalogSyncTask(
        dispatch_engine=MagicMock(),
        mirror_path=mirror,
        summary_backend=backend,  # type: ignore[arg-type]
    )
    assert await task.summarise_synced() is None
    assert backend.calls == []


@pytest.mark.asyncio
async def test_summarise_synced_without_a_backend_names_the_skill(tmp_path: Path) -> None:
    from persona.skills.summary import SkillSummaryCache

    mirror, _ = _over_budget_mirror(tmp_path)
    cache = SkillSummaryCache()
    task = SkillCatalogSyncTask(
        dispatch_engine=MagicMock(), mirror_path=mirror, summary_cache=cache, summary_backend=None
    )
    report = await task.summarise_synced()
    assert report is not None
    assert report.unavailable == ("huge",)
    assert len(cache) == 0


def test_build_composes_the_cache_beside_the_mirror(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from types import SimpleNamespace

    from persona.skills.summary import SkillSummaryCache
    from persona_api.jobs.skill_catalog_sync import build_skill_catalog_sync

    monkeypatch.setenv("PERSONA_SKILL_MIRROR_PATH", str(tmp_path / "vol" / "skill_mirror.json"))
    monkeypatch.delenv("PERSONA_SKILL_SUMMARY_CACHE_PATH", raising=False)
    monkeypatch.delenv("PERSONA_SKILL_SUMMARIES_ENABLED", raising=False)
    config = SimpleNamespace(
        skill_catalog_sync_enabled=True,
        skill_openclaw_repo_url="",
        skill_openclaw_ref="",
        skill_github_repos_parsed=(),
    )
    task = build_skill_catalog_sync(config, dispatch_engine=MagicMock())  # type: ignore[arg-type]
    assert task is not None
    cache = task._summary_cache  # noqa: SLF001
    assert isinstance(cache, SkillSummaryCache)
    assert cache.path == tmp_path / "vol" / "skill_summaries.json"

    shared = SkillSummaryCache()
    task2 = build_skill_catalog_sync(
        config,  # type: ignore[arg-type]
        dispatch_engine=MagicMock(),
        summary_cache=shared,
    )
    assert task2 is not None
    assert task2._summary_cache is shared  # noqa: SLF001

    monkeypatch.setenv("PERSONA_SKILL_SUMMARIES_ENABLED", "false")
    task3 = build_skill_catalog_sync(config, dispatch_engine=MagicMock())  # type: ignore[arg-type]
    assert task3 is not None
    assert task3._summary_cache is None  # noqa: SLF001


class _StubSync:
    """A sync task double for the worker step: sync ``run_once``, async ``summarise_synced``."""

    def __init__(self, result: SkillMirrorSyncResult | None) -> None:
        self._result = result
        self.summarised = 0

    def run_once(self) -> SkillMirrorSyncResult | None:
        return self._result

    async def summarise_synced(self) -> None:
        self.summarised += 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "expected"),
    [(_synced_result(), 1), (None, 0)],
    ids=["leader-synced", "not-leader-or-failed"],
)
async def test_worker_summarises_only_after_a_completed_sync(
    result: SkillMirrorSyncResult | None, expected: int
) -> None:
    from persona.jobs import JobRegistry
    from persona_api.jobs import Worker

    stub = _StubSync(result)
    worker = Worker(
        dispatch_engine=MagicMock(),
        rls_engine=MagicMock(),
        registry=JobRegistry(),
        worker_id="w-test",
        skill_catalog_sync=stub,  # type: ignore[arg-type]
    )
    await worker._maybe_run_skill_catalog_sync()  # noqa: SLF001
    assert stub.summarised == expected
