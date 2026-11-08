"""The Images workflow builds the api image for amd64 and arm64, smoke-tests it, pushes nothing.

The api image must build natively on the Oracle server (arm64) and stay buildable on
amd64. The repository is public, so GitHub's ``ubuntu-24.04-arm`` runners are free. The
workflow is a build check only: no registry is decided yet (GHCR by digest is a
follow-up after go-live, ruling D2), so it holds no secret, logs in nowhere and pushes
nothing. It is not the Deploy workflow and ``scripts/check_deploy_wiring.py`` does not
read it; this test is its gate.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "images.yml"
RUNNERS = {"ubuntu-24.04", "ubuntu-24.04-arm"}
#: actions/checkout v4.4.0, the same commit deploy.yml pins.
CHECKOUT = "actions/checkout@11d5960a326750d5838078e36cf38b85af677262"


def _text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _workflow() -> dict[Any, Any]:
    return yaml.safe_load(_text())


def _triggers(workflow: dict[Any, Any]) -> dict[str, Any]:
    # YAML 1.1 reads the key `on` as True.
    return workflow.get("on", workflow.get(True)) or {}


def _steps() -> list[dict[str, Any]]:
    return [s for job in _workflow()["jobs"].values() for s in job.get("steps", [])]


def _run_text() -> str:
    return "\n".join(str(s.get("run", "")) for s in _steps())


def test_it_runs_on_pull_requests_and_by_hand_only() -> None:
    triggers = _triggers(_workflow())
    assert set(triggers) == {"pull_request", "workflow_dispatch"}
    paths = triggers["pull_request"]["paths"]
    for needed in (
        "uv.lock",
        "packages/api/Dockerfile",
        "packages/connectors/pyproject.toml",
        "packages/core/src/persona/stores/embedder.py",
        ".github/workflows/images.yml",
    ):
        assert any(fnmatch.fnmatch(needed, p) for p in paths), f"a change to {needed} skips it"


def test_it_can_only_read_the_repository() -> None:
    workflow = _workflow()
    assert workflow["permissions"] == {"contents": "read"}
    for name, job in workflow["jobs"].items():
        assert "permissions" not in job, f"job {name} widens the token"


def test_it_holds_no_secret_and_pushes_nowhere() -> None:
    text = _text()
    assert not re.search(r"(?i)\bsecrets\b", text)
    assert not re.search(r"docker\s+(login|push)|--push\b|buildx\s+imagetools", text)
    assert not re.search(r"docker/(login|build-push)-action", text)


def test_every_action_is_pinned_by_commit() -> None:
    uses = [s["uses"] for s in _steps() if "uses" in s]
    assert uses, "expected a checkout step"
    assert all(u == CHECKOUT for u in uses), uses


def test_it_builds_on_both_architectures() -> None:
    jobs = _workflow()["jobs"]
    runners = {r for job in jobs.values() for r in job["strategy"]["matrix"]["runner"]}
    assert runners == RUNNERS


def test_it_builds_the_api_image_and_smoke_tests_it() -> None:
    run = _run_text()
    assert re.search(r"docker build\b.*-f packages/api/Dockerfile", run.replace("\\\n", " "))
    # The image must import the api and the connectors it hosts, carry CPU torch, and
    # NOT carry voice (the api never imports it).
    for needle in (
        "persona_api.app",
        "persona_connectors.service",
        "torch.cuda.is_available()",
        "+cpu",
        "persona_voice",
    ):
        assert needle in run, f"the smoke test does not check {needle}"
    assert "MAX_IMAGE_BYTES" in run
    # The server kit runs the image as the data uid, not the image's own user.
    assert "--user 2001:2001" in run


def test_the_build_check_is_not_the_deploy_workflow() -> None:
    assert WORKFLOW.name != "deploy.yml"
    assert "flyctl" not in _text()
