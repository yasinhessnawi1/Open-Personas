"""The deploy wiring gate, run against the REAL Deploy workflow broken one way at a time.

R9-213 part 2: the api and voice get the same seven settings from the ``production``
Environment; the settings are checked in a job that never holds the Fly token; and no
secret may reach anywhere but its allowed site. Each test here loads
``.github/workflows/deploy.yml``, breaks it in one of the ways the security reviews
listed, and asserts the exact problems the gate reports. The gate's own self-test does
the same on a sample workflow every time it runs; these prove it on the file that
actually deploys.
"""

from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import yaml
from persona_runtime.chain_report import UNIFIED_ENV_NAMES

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType

_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _ROOT / ".github" / "workflows" / "deploy.yml"
_MID = "PERSONA_MID_MODELS"
_HERE = "not the step the allow-list has here"
_OUTSIDE = "a secret outside its allowed site"
_TOOLING = "Python tooling in a job that holds FLY_API_TOKEN"
_KEY = "is not allowed on a job that holds a secret"


@pytest.fixture(scope="module")
def gate() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_deploy_wiring", _ROOT / "scripts" / "check_deploy_wiring.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_deploy_wiring"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def check(gate: ModuleType) -> Callable[[dict[Any, Any]], list[str]]:
    gate_problems = gate.load_fork_gate().gate_problems

    def _check(workflow: dict[Any, Any]) -> list[str]:
        problems: list[str] = gate.check_deploy_wiring(workflow, gate_problems)
        return problems

    return _check


@pytest.fixture
def workflow() -> dict[Any, Any]:
    loaded: dict[Any, Any] = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    return copy.deepcopy(loaded)


def _index(workflow: dict[Any, Any], job: str, marker: str) -> int:
    steps = workflow["jobs"][job]["steps"]
    found = [i for i, step in enumerate(steps) if marker in str(step.get("run", ""))]
    assert len(found) == 1
    return found[0]


def test_the_real_deploy_workflow_is_clean(check: Any, workflow: dict[Any, Any]) -> None:  # noqa: ANN401
    assert check(workflow) == []


def test_the_gate_self_test_and_the_real_workflow_both_pass(gate: ModuleType) -> None:
    assert gate.main() == 0


def test_its_list_of_the_seven_is_the_runtimes(gate: ModuleType) -> None:
    assert gate.SHARED == UNIFIED_ENV_NAMES


def test_every_secret_holding_job_in_the_workflow_has_an_allow_list(
    gate: ModuleType, workflow: dict[Any, Any]
) -> None:
    holders = {name for name, job in workflow["jobs"].items() if gate._holds_secret(job)}
    assert holders == set(gate.ALLOWED_STEPS)


def test_the_token_is_never_in_the_validate_job(workflow: dict[Any, Any]) -> None:
    assert "FLY_API_TOKEN" not in yaml.safe_dump(workflow["jobs"]["validate-model-chains"])


# ---- item 1: the job, not the step, is the boundary ---------------------------------------


def test_uv_before_the_stage_step_is_caught(check: Any, workflow: dict[Any, Any]) -> None:  # noqa: ANN401
    index = _index(workflow, "stage-model-chains", "fly_deploy_chains.py stage")
    workflow["jobs"]["stage-model-chains"]["steps"].insert(index, {"run": "uv sync --frozen"})
    assert check(workflow) == [
        f"jobs.stage-model-chains.steps[{index}]: {_HERE}",
        f"jobs.stage-model-chains.steps[{index}]: {_TOOLING}",
    ]


def test_setup_python_in_a_token_job_is_caught(check: Any, workflow: dict[Any, Any]) -> None:  # noqa: ANN401
    workflow["jobs"]["verify-model-chains"]["steps"].insert(1, {"uses": "actions/setup-python@v5"})
    assert check(workflow) == [
        f"jobs.verify-model-chains.steps[1]: {_HERE}",
        f"jobs.verify-model-chains.steps[1]: {_TOOLING}",
    ]


def test_the_token_in_the_validate_job_is_caught(check: Any, workflow: dict[Any, Any]) -> None:  # noqa: ANN401
    index = _index(workflow, "validate-model-chains", "validate_deploy_chains.py")
    step = workflow["jobs"]["validate-model-chains"]["steps"][index]
    step["env"]["FLY_API_TOKEN"] = "${{ secrets.FLY_API_TOKEN }}"
    assert check(workflow) == [
        f"jobs.validate-model-chains.steps[{index}]: {_HERE}",
        f"jobs.validate-model-chains.steps[{index}].env.FLY_API_TOKEN: {_OUTSIDE}",
        f"jobs.validate-model-chains.steps[1]: {_TOOLING}",
        f"jobs.validate-model-chains.steps[2]: {_TOOLING}",
        f"jobs.validate-model-chains.steps[3]: {_TOOLING}",
    ]


def test_a_deploy_that_does_not_wait_for_the_stage_job_is_caught(
    check: Any,  # noqa: ANN401
    workflow: dict[Any, Any],
) -> None:
    workflow["jobs"]["deploy-api"]["needs"] = "validate-model-chains"
    assert check(workflow) == ["jobs.deploy-api: does not need stage-model-chains"]


def test_a_verify_job_that_does_not_need_the_stage_job_is_caught(
    check: Any,  # noqa: ANN401
    workflow: dict[Any, Any],
) -> None:
    workflow["jobs"]["verify-model-chains"]["needs"] = ["deploy-api", "deploy-voice"]
    assert check(workflow) == ["jobs.verify-model-chains: does not need stage-model-chains"]


# ---- item 2: exact token sites, and no script that reads the token or a setting -----------


def test_a_token_step_that_echoes_part_of_the_token_is_caught(
    check: Any,  # noqa: ANN401
    workflow: dict[Any, Any],
) -> None:
    index = _index(workflow, "stage-model-chains", "fly_deploy_chains.py stage")
    workflow["jobs"]["stage-model-chains"]["steps"][index]["run"] = (
        "flyctl version\necho ${FLY_API_TOKEN:0:8}\n"
    )
    assert check(workflow) == [
        f"jobs.stage-model-chains.steps[{index}]: {_HERE}",
        f"jobs.stage-model-chains.steps[{index}]: reads FLY_API_TOKEN or a PERSONA_ setting "
        "in its script",
    ]


def test_an_edited_flyctl_deploy_command_is_caught(check: Any, workflow: dict[Any, Any]) -> None:  # noqa: ANN401
    index = _index(workflow, "deploy-livekit", "flyctl deploy")
    step = workflow["jobs"]["deploy-livekit"]["steps"][index]
    step["run"] = step["run"].replace("--wait-timeout 300", "--wait-timeout 300 --verbose")
    assert check(workflow) == [f"jobs.deploy-livekit.steps[{index}]: {_HERE}"]


# ---- the review's bypass list ---------------------------------------------------------------


def test_an_extra_step_echoing_a_fragment_is_caught(check: Any, workflow: dict[Any, Any]) -> None:  # noqa: ANN401
    steps = workflow["jobs"]["stage-model-chains"]["steps"]
    steps.append({"env": {_MID: f"${{{{ secrets.{_MID} }}}}"}, "run": f"echo ${{{_MID}:0:6}}"})
    last = len(steps) - 1
    assert check(workflow) == [
        f"jobs.stage-model-chains.steps[{last}]: {_HERE}",
        f"jobs.stage-model-chains.steps[{last}]: reads FLY_API_TOKEN or a PERSONA_ setting "
        "in its script",
    ]


def test_to_json_of_secrets_is_caught(check: Any, workflow: dict[Any, Any]) -> None:  # noqa: ANN401
    index = _index(workflow, "deploy-api", "flyctl deploy")
    workflow["jobs"]["deploy-api"]["steps"][index]["run"] += "echo '${{ toJSON(secrets) }}'\n"
    assert check(workflow) == [
        f"jobs.deploy-api.steps[{index}]: {_HERE}",
        f"jobs.deploy-api.steps[{index}].run: uses toJSON(secrets)",
    ]


def test_bracket_access_to_secrets_is_caught(check: Any, workflow: dict[Any, Any]) -> None:  # noqa: ANN401
    index = _index(workflow, "deploy-voice", "flyctl deploy")
    workflow["jobs"]["deploy-voice"]["steps"][index]["env"]["X"] = "${{ secrets['FLY_API_TOKEN'] }}"
    assert check(workflow) == [
        f"jobs.deploy-voice.steps[{index}]: {_HERE}",
        f"jobs.deploy-voice.steps[{index}].env.X: reads secrets by bracket",
    ]


def test_a_secret_in_job_level_env_is_caught(check: Any, workflow: dict[Any, Any]) -> None:  # noqa: ANN401
    workflow["jobs"]["deploy-livekit"]["env"] = {"FLY_API_TOKEN": "${{ secrets.FLY_API_TOKEN }}"}
    assert check(workflow) == [
        f"jobs.deploy-livekit: key env {_KEY}",
        f"jobs.deploy-livekit.env.FLY_API_TOKEN: {_OUTSIDE}",
    ]


def test_a_shell_override_that_traces_is_caught(check: Any, workflow: dict[Any, Any]) -> None:  # noqa: ANN401
    index = _index(workflow, "stage-model-chains", "fly_deploy_chains.py stage")
    workflow["jobs"]["stage-model-chains"]["steps"][index]["shell"] = "bash -x {0}"
    assert check(workflow) == [
        f"jobs.stage-model-chains.steps[{index}]: {_HERE}",
        f"jobs.stage-model-chains.steps[{index}]: overrides the shell",
    ]


def test_continue_on_error_is_caught(check: Any, workflow: dict[Any, Any]) -> None:  # noqa: ANN401
    workflow["jobs"]["stage-model-chains"]["continue-on-error"] = True
    assert check(workflow) == [f"jobs.stage-model-chains: key continue-on-error {_KEY}"]


def test_if_always_is_caught(check: Any, workflow: dict[Any, Any]) -> None:  # noqa: ANN401
    workflow["jobs"]["deploy-voice"]["if"] = "always()"
    assert check(workflow) == ["deploy-voice: expected exactly two paths, got 1"]


def test_a_job_with_no_if_is_caught(check: Any, workflow: dict[Any, Any]) -> None:  # noqa: ANN401
    del workflow["jobs"]["verify-model-chains"]["if"]
    assert check(workflow) == ["verify-model-chains has no gate at all"]


def test_a_secret_holding_job_outside_the_production_environment_is_caught(
    check: Any,  # noqa: ANN401
    workflow: dict[Any, Any],
) -> None:
    del workflow["jobs"]["validate-model-chains"]["environment"]
    assert check(workflow) == [
        "jobs.validate-model-chains: holds a secret without environment: production"
    ]


def test_a_setting_mapped_to_another_secret_is_caught(check: Any, workflow: dict[Any, Any]) -> None:  # noqa: ANN401
    index = _index(workflow, "stage-model-chains", "fly_deploy_chains.py stage")
    workflow["jobs"]["stage-model-chains"]["steps"][index]["env"][_MID] = (
        "${{ secrets.PERSONA_SMALL_MODELS }}"
    )
    assert check(workflow) == [
        f"jobs.stage-model-chains.steps[{index}]: {_HERE}",
        f"jobs.stage-model-chains.steps[{index}].env.{_MID}: {_OUTSIDE}",
    ]


def test_losing_the_workflow_concurrency_group_is_caught(
    check: Any,  # noqa: ANN401
    workflow: dict[Any, Any],
) -> None:
    del workflow["concurrency"]
    assert check(workflow) == [
        "workflow: no single concurrency group with cancel-in-progress: false"
    ]


# ---- round 4: job-level and workflow-level keys --------------------------------------------


def test_a_container_on_the_stage_job_is_caught(check: Any, workflow: dict[Any, Any]) -> None:  # noqa: ANN401
    """Every step, its python3 included, would run inside a third-party image with the token."""
    workflow["jobs"]["stage-model-chains"]["container"] = "python:3.12"
    assert check(workflow) == [f"jobs.stage-model-chains: key container {_KEY}"]


def test_services_on_a_deploy_job_are_caught(check: Any, workflow: dict[Any, Any]) -> None:  # noqa: ANN401
    workflow["jobs"]["deploy-api"]["services"] = {"cache": {"image": "redis:7"}}
    assert check(workflow) == [f"jobs.deploy-api: key services {_KEY}"]


def test_a_job_level_bash_env_is_caught(check: Any, workflow: dict[Any, Any]) -> None:  # noqa: ANN401
    workflow["jobs"]["stage-model-chains"]["env"] = {"BASH_ENV": "/tmp/hook.sh"}
    assert check(workflow) == [f"jobs.stage-model-chains: key env {_KEY}"]


def test_a_workflow_level_ld_preload_is_caught(check: Any, workflow: dict[Any, Any]) -> None:  # noqa: ANN401
    workflow["env"] = {"LD_PRELOAD": "/tmp/hook.so"}
    assert check(workflow) == ["workflow: key env is not allowed"]


def test_a_self_hosted_runner_is_caught(check: Any, workflow: dict[Any, Any]) -> None:  # noqa: ANN401
    workflow["jobs"]["deploy-voice"]["runs-on"] = "self-hosted"
    assert check(workflow) == ["jobs.deploy-voice: runs-on must be ubuntu-latest"]


def test_job_level_permissions_are_caught(check: Any, workflow: dict[Any, Any]) -> None:  # noqa: ANN401
    workflow["jobs"]["verify-model-chains"]["permissions"] = {
        "contents": "write",
        "id-token": "write",
    }
    assert check(workflow) == [f"jobs.verify-model-chains: key permissions {_KEY}"]


def test_a_per_job_concurrency_group_is_caught(check: Any, workflow: dict[Any, Any]) -> None:  # noqa: ANN401
    workflow["jobs"]["deploy-livekit"]["concurrency"] = {"group": "x", "cancel-in-progress": True}
    assert check(workflow) == [f"jobs.deploy-livekit: key concurrency {_KEY}"]


def test_every_job_in_the_workflow_carries_only_allowed_keys(
    gate: ModuleType, workflow: dict[Any, Any]
) -> None:
    for job in workflow["jobs"].values():
        assert set(job) <= gate.SECRET_JOB_KEYS
        assert job["runs-on"] == "ubuntu-latest"


# ---- item 3: reusable workflows ------------------------------------------------------------


def test_a_reusable_workflow_that_inherits_the_secrets_is_caught(
    check: Any,  # noqa: ANN401
    workflow: dict[Any, Any],
) -> None:
    workflow["jobs"]["reuse"] = {
        "if": workflow["jobs"]["deploy-api"]["if"],
        "uses": "someone/else/.github/workflows/x.yml@main",
        "secrets": "inherit",
    }
    assert check(workflow) == [
        "jobs.reuse.secrets: passes secrets on (secrets:)",
        "jobs.reuse: calls a reusable workflow",
    ]


# ---- item 4: failure swallowing ------------------------------------------------------------


@pytest.mark.parametrize(
    "tail",
    ["|| true", "|| :", "|| exit", "|| exit 0", "; exit 0", "\nset +e", "\ntrap 'exit 0' ERR"],
)
def test_every_way_of_swallowing_a_failure_is_caught(
    check: Any,  # noqa: ANN401
    workflow: dict[Any, Any],
    tail: str,
) -> None:
    index = _index(workflow, "deploy-api", "curl")
    step = workflow["jobs"]["deploy-api"]["steps"][index]
    step["run"] = step["run"].rstrip("\n") + f" {tail}\n"
    assert check(workflow) == [
        f"jobs.deploy-api.steps[{index}]: {_HERE}",
        f"jobs.deploy-api.steps[{index}]: swallows a failure",
    ]


def test_exit_1_after_a_failure_is_not_swallowing(gate: ModuleType) -> None:
    """``|| exit 1`` and a bare ``exit 0`` line (the smoke tests' success path) are fine."""
    assert gate._SWALLOWED.search("curl x || exit 1\n") is None
    assert gate._SWALLOWED.search('echo "ok"\n    exit 0\n') is None
