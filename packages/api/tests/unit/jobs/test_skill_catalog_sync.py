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

from persona.skills.skill_mirror_reconcile import SkillMirrorSyncResult
from persona_api.jobs.catalog_sync import CATALOG_SYNC_LEADER_LOCK_KEY
from persona_api.jobs.skill_catalog_sync import (
    SKILL_CATALOG_SYNC_LEADER_LOCK_KEY,
    SkillCatalogSyncTask,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


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
