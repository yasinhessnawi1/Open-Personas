#!/usr/bin/env python
"""The Deploy workflow hands both apps the shared settings, and no secret goes anywhere else.

R9-213 part 2. The api and voice must be handed the same seven settings (the six model
chains and the OpenRouter subscription mode, ``UNIFIED_ENV_NAMES`` in
``persona_runtime.chain_report``) from one source, the ``production`` Environment's
secrets. ``.github/workflows/deploy.yml`` checks them in ``validate-model-chains`` (a job
that never holds the Fly token), stages them on both apps in ``stage-model-chains`` and
compares them in ``verify-model-chains``. This gate reads the workflow and fails on
anything that breaks that or lets a secret out.

On a runner the JOB, not the step, is the boundary: an earlier step can read the runner
process, write ``$GITHUB_ENV`` or ``$GITHUB_PATH``, or overwrite a script before a later
step runs. So every job that holds a secret has an exact allow-list of steps
(``ALLOWED_STEPS``): each step, minus its display name, must equal the allow-listed step
at its position, run text included. The Fly token lives only in jobs whose steps are
checkout, the pinned flyctl setup and exact flyctl or standard-library commands.

Refused wherever they appear:

* a ``secrets`` reference anywhere but ``NAME: ${{ secrets.NAME }}`` in a step ``env``, for
  a name that job's allow-list gives it; so ``toJSON(secrets)``, bracket access, secrets in
  workflow-level or job-level ``env``, in a ``run`` block or in an input all fail;
* a ``run`` block that reads ``$FLY_API_TOKEN``, ``${FLY_API_TOKEN``, ``$PERSONA_`` or
  ``${PERSONA_`` (flyctl reads its token from its environment; nothing else needs it);
* ``uv``, ``pip``, ``setup-uv`` or ``setup-python`` in a job that holds the Fly token;
* ``shell:`` overrides, ``defaults``, ``continue-on-error``, a reusable workflow (job-level
  ``uses:``) and any ``secrets:`` key (``secrets: inherit`` included);
* failure swallowing (``|| true``, ``|| :``, ``|| exit``, ``|| exit 0``, ``; exit 0``,
  ``set +e``, ``trap``) and command tracing;
* a job that holds a secret without ``environment: production``, and any job whose ``if``
  fails the gate the fork test enforces, reusing its parser (``gate_problems`` in
  ``packages/api/tests/unit/test_deploy_fork_gate.py``) rather than a second one.

It proves itself on every run: ``FIXTURES`` break a sample workflow in each of those ways,
through the same :func:`check_deploy_wiring` that decides the real verdict.
"""

from __future__ import annotations

import copy
import importlib.util
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping
    from types import ModuleType

    GateCheck = Callable[[str, object], list[str]]


def _repo_root() -> Path:
    """The worktree being checked, asked of git rather than assumed (see check_flags_declared)."""
    import subprocess

    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
        )
        return Path(out.stdout.strip())
    except Exception:  # noqa: BLE001 - any git failure falls back to the script's own path
        return Path(__file__).resolve().parent.parent


ROOT = _repo_root()
DEPLOY_WORKFLOW = ROOT / ".github" / "workflows" / "deploy.yml"
FORK_GATE_TEST = ROOT / "packages" / "api" / "tests" / "unit" / "test_deploy_fork_gate.py"
VALIDATE_JOB = "validate-model-chains"
STAGE_JOB = "stage-model-chains"
VERIFY_JOB = "verify-model-chains"
DEPLOY_JOBS = ("deploy-api", "deploy-voice")
ENVIRONMENT = "production"
TOKEN = "FLY_API_TOKEN"
SHARED = (
    "PERSONA_FRONTIER_MODELS",
    "PERSONA_MID_MODELS",
    "PERSONA_SMALL_MODELS",
    "PERSONA_FREE_FRONTIER_MODELS",
    "PERSONA_FREE_MID_MODELS",
    "PERSONA_FREE_SMALL_MODELS",
    "PERSONA_OPENROUTER_SUBSCRIPTION_MODE",
)

#: The only keys a job that holds a secret may carry. ``timeout-minutes`` is allowed because
#: it can only cut a job short, which fails loudly. Every other key can change what runs next
#: to the secret or where it goes: ``container`` and ``services`` run third-party images beside
#: it, ``env`` can set ``BASH_ENV`` or ``LD_PRELOAD``, ``permissions`` widens the token,
#: ``outputs`` exports values, ``strategy`` multiplies the job, ``concurrency`` could cancel a
#: stage half done, and ``defaults``, ``continue-on-error``, ``uses`` and ``secrets`` are
#: refused everywhere anyway.
SECRET_JOB_KEYS = frozenset(
    {"name", "if", "needs", "runs-on", "environment", "steps", "timeout-minutes"}
)
RUNNER = "ubuntu-latest"
#: The only top-level keys the workflow may carry (``on`` parses as ``True`` under YAML 1.1).
#: Workflow-level ``env`` and ``defaults`` reach every job, so neither is allowed.
WORKFLOW_KEYS = frozenset({"name", "run-name", "on", True, "permissions", "concurrency", "jobs"})
WORKFLOW_PERMISSIONS = {"contents": "read"}

_EXPRESSION = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)
_SECRETS = re.compile(r"(?i)\bsecrets\b")
_TO_JSON = re.compile(r"(?i)\btojson\s*\(\s*secrets\s*\)")
_BRACKET = re.compile(r"(?i)\bsecrets\s*\[")
_WHOLE_REF = re.compile(r"\$\{\{\s*secrets\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_READS_SETTING = re.compile(r"\$\{?(?:FLY_API_TOKEN|PERSONA_)")
_PYTHON_TOOLING = re.compile(r"(?m)(?:^|[\s;&|(])(?:uv|uvx|pip|pip3)(?=\s|$)|-m\s+pip\b")
_TRACING = re.compile(r"\bset\s+(?:-[A-Za-z]*x|-o\s+xtrace)")
_SWALLOWED = re.compile(
    r"\|\|\s*(?:true|:)(?![\w-])"
    r"|\|\|\s*exit(?:\s+0)?[ \t]*(?:$|[;)&|])"
    r"|;\s*exit\s+0\b"
    r"|\bset\s+\+[A-Za-z]*e"
    r"|\btrap\b",
    re.MULTILINE,
)

for pkg in ("core", "runtime"):
    sys.path.insert(0, str(ROOT / "packages" / pkg / "src"))


# ---- the allow-list: every step of every job that holds a secret ---------------------------


def _secret(name: str) -> str:
    return f"${{{{ secrets.{name} }}}}"


_CHECKOUT = {
    "uses": "actions/checkout@11d5960a326750d5838078e36cf38b85af677262",
    "with": {
        "ref": "${{ github.event.workflow_run.head_sha || github.sha }}",
        "persist-credentials": False,
    },
}
_SETUP_FLYCTL = {
    "uses": "superfly/flyctl-actions/setup-flyctl@ed8efb33836e8b2096c7fd3ba1c8afe303ebbff1",
    "with": {"version": "0.4.108"},
}
_SETUP_UV = {
    "uses": "astral-sh/setup-uv@c18668ad3cf93ea998bef934396af7bb5c839dc7",
    "with": {"version": "0.12.19", "enable-cache": False},
}
_TOKEN_ENV = {TOKEN: _secret(TOKEN)}
_SHARED_ENV = {name: _secret(name) for name in SHARED}
_APPS = "open-persona-api open-persona-voice"


def _flyctl_deploy(config: str, app: str, wait: int) -> str:
    return (
        "flyctl deploy \\\n  --remote-only \\\n"
        f"  --config {config} \\\n  --app {app} \\\n  --strategy rolling \\\n"
        f"  --wait-timeout {wait}\n"
    )


def _smoke(url_check: str, ok: str, waiting: str, failure: str, preamble: str = "") -> str:
    return (
        f'{preamble}for i in 1 2 3 4 5 6; do\n  if {url_check}; then\n    echo "{ok}"\n'
        f'    exit 0\n  fi\n  echo "{waiting} ($i/6)…"\n  sleep 10\ndone\n'
        f'echo "::error::{failure}"\nexit 1\n'
    )


#: Each job that may hold a secret, and every one of its steps (minus ``name``), in order.
ALLOWED_STEPS: dict[str, list[dict[str, Any]]] = {
    VALIDATE_JOB: [
        _CHECKOUT,
        _SETUP_UV,
        {"run": "uv sync --frozen --no-dev --package persona-runtime"},
        {
            "env": _SHARED_ENV,
            "run": "set -euo pipefail\nuv run --no-sync python scripts/validate_deploy_chains.py\n",
        },
    ],
    STAGE_JOB: [
        _CHECKOUT,
        _SETUP_FLYCTL,
        {
            "env": {**_TOKEN_ENV, **_SHARED_ENV},
            "run": f"set -euo pipefail\npython3 -I scripts/fly_deploy_chains.py stage {_APPS}\n",
        },
    ],
    "deploy-api": [
        _CHECKOUT,
        _SETUP_FLYCTL,
        {
            "env": {**_TOKEN_ENV, "WORKSPACE": "${{ github.workspace }}"},
            "run": _flyctl_deploy('"$WORKSPACE/packages/api/fly.toml"', "open-persona-api", 600),
        },
        {
            "run": _smoke(
                "curl -fsS --max-time 10 https://api.openpersona.online/healthz | "
                'grep -q \'"status":"ok"\'',
                "healthz ok",
                "waiting for healthz to flip green",
                "healthz did not flip green within 60s after deploy",
                "# Give the new machine a moment to settle, then verify the public URL.\n",
            )
        },
    ],
    "deploy-voice": [
        _CHECKOUT,
        _SETUP_FLYCTL,
        {
            "env": {**_TOKEN_ENV, "WORKSPACE": "${{ github.workspace }}"},
            "run": _flyctl_deploy(
                '"$WORKSPACE/packages/voice/fly.toml"', "open-persona-voice", 900
            ),
        },
        {"if": "success()", "env": _TOKEN_ENV, "run": "flyctl status --app open-persona-voice\n"},
    ],
    "deploy-livekit": [
        _CHECKOUT,
        _SETUP_FLYCTL,
        {
            "working-directory": "packages/livekit",
            "env": _TOKEN_ENV,
            "run": _flyctl_deploy("fly.toml", "open-persona-livekit", 300),
        },
        {
            "run": _smoke(
                "curl -fsS --max-time 10 https://open-persona-livekit.fly.dev/ >/dev/null",
                "livekit signaling reachable",
                "waiting for livekit signaling",
                "livekit signaling did not respond within 60s after deploy",
            )
        },
    ],
    VERIFY_JOB: [
        _CHECKOUT,
        _SETUP_FLYCTL,
        {
            "env": _TOKEN_ENV,
            "run": f"set -euo pipefail\npython3 -I scripts/fly_deploy_chains.py verify {_APPS}\n",
        },
    ],
}


def _allowed_secret_names(job: str) -> set[str]:
    return {
        name
        for step in ALLOWED_STEPS.get(job, [])
        for name, value in (step.get("env") or {}).items()
        if value == _secret(name)
    }


# ---- walking the workflow ----------------------------------------------------------------


def _where(path: tuple[Any, ...]) -> str:
    text = ""
    for part in path:
        text += f"[{part}]" if isinstance(part, int) else f".{part}"
    return text.lstrip(".") or "workflow"


def _nodes(node: Any, path: tuple[Any, ...] = ()) -> Iterator[tuple[tuple[Any, ...], Any]]:  # noqa: ANN401
    """Every value in the workflow, with where it sits."""
    yield path, node
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _nodes(value, (*path, key))
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from _nodes(item, (*path, index))


def _jobs(workflow: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    jobs = workflow.get("jobs") or {}
    return {str(name): job for name, job in jobs.items() if isinstance(job, dict)}


def _holds_secret(node: Any, name: str | None = None) -> bool:  # noqa: ANN401
    """Whether any expression under ``node`` names secrets (or the secret ``name``)."""
    for _, value in _nodes(node):
        if isinstance(value, str):
            for expression in _EXPRESSION.findall(value):
                if _SECRETS.search(expression) and (name is None or name in expression):
                    return True
    return False


def _needs(job: Mapping[str, Any]) -> set[str]:
    needs = job.get("needs")
    return {needs} if isinstance(needs, str) else set(needs or [])


# ---- the checks --------------------------------------------------------------------------


def _structure_problems(workflow: Mapping[str, Any]) -> list[str]:
    jobs = _jobs(workflow)
    required = {
        VALIDATE_JOB: set(),
        STAGE_JOB: {VALIDATE_JOB},
        **{name: {STAGE_JOB} for name in DEPLOY_JOBS},
        VERIFY_JOB: {STAGE_JOB, *DEPLOY_JOBS},
    }
    problems: list[str] = []
    for name, needed in required.items():
        job = jobs.get(name)
        if job is None:
            problems.append(f"jobs.{name}: missing")
            continue
        problems.extend(
            f"jobs.{name}: does not need {need}" for need in sorted(needed - _needs(job))
        )
    return problems


def _allow_list_problems(workflow: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    for name, job in _jobs(workflow).items():
        if not _holds_secret(job):
            continue
        allowed = ALLOWED_STEPS.get(name)
        if allowed is None:
            problems.append(f"jobs.{name}: holds a secret but has no step allow-list")
            continue
        steps = [{k: v for k, v in step.items() if k != "name"} for step in job.get("steps") or []]
        first = next(
            (i for i, (a, b) in enumerate(zip(steps, allowed, strict=False)) if a != b), None
        )
        if first is None and len(steps) > len(allowed):
            first = len(allowed)
        if first is not None:
            problems.append(f"jobs.{name}.steps[{first}]: not the step the allow-list has here")
        elif len(steps) < len(allowed):
            problems.append(f"jobs.{name}: stops before allow-listed step {len(steps)}")
    return problems


def _key_problems(workflow: Mapping[str, Any]) -> list[str]:
    problems = [
        f"workflow: key {key} is not allowed" for key in workflow if key not in WORKFLOW_KEYS
    ]
    if workflow.get("permissions") != WORKFLOW_PERMISSIONS:
        problems.append("workflow: permissions must be exactly contents: read")
    for name, job in _jobs(workflow).items():
        if not _holds_secret(job):
            if "permissions" in job:
                problems.append(f"jobs.{name}: sets its own permissions")
            continue
        problems.extend(
            f"jobs.{name}: key {key} is not allowed on a job that holds a secret"
            for key in job
            if key not in SECRET_JOB_KEYS
        )
        if job.get("runs-on") != RUNNER:
            problems.append(f"jobs.{name}: runs-on must be {RUNNER}")
    return problems


def _allowed_site(path: tuple[Any, ...], name: str) -> bool:
    shape = len(path) == 6 and path[0] == "jobs" and path[2] == "steps" and path[4] == "env"
    return shape and path[5] == name and name in _allowed_secret_names(str(path[1]))


def _secret_problems(workflow: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    for path, value in _nodes(workflow):
        if isinstance(value, dict) and "secrets" in value:
            problems.append(f"{_where((*path, 'secrets'))}: passes secrets on (secrets:)")
        if not isinstance(value, str):
            continue
        expressions = [value] if path and path[-1] == "if" else _EXPRESSION.findall(value)
        if not any(_SECRETS.search(expression) for expression in expressions):
            continue
        where = _where(path)
        if _TO_JSON.search(value):
            problems.append(f"{where}: uses toJSON(secrets)")
        elif _BRACKET.search(value):
            problems.append(f"{where}: reads secrets by bracket")
        else:
            ref = _WHOLE_REF.fullmatch(value.strip())
            if ref is None or not _allowed_site(path, ref.group(1)):
                problems.append(f"{where}: a secret outside its allowed site")
    return problems


def _step_problems(name: str, index: int, step: Mapping[str, Any], token_job: bool) -> list[str]:
    where = f"jobs.{name}.steps[{index}]"
    run = str(step.get("run", ""))
    uses = str(step.get("uses", ""))
    problems: list[str] = []
    if "shell" in step:
        problems.append(f"{where}: overrides the shell")
    if "continue-on-error" in step:
        problems.append(f"{where}: continue-on-error")
    if _TRACING.search(run):
        problems.append(f"{where}: turns on command tracing")
    if _SWALLOWED.search(run):
        problems.append(f"{where}: swallows a failure")
    if _READS_SETTING.search(run):
        problems.append(f"{where}: reads FLY_API_TOKEN or a PERSONA_ setting in its script")
    tooling = "setup-uv" in uses or "setup-python" in uses or _PYTHON_TOOLING.search(run)
    if token_job and tooling:
        problems.append(f"{where}: Python tooling in a job that holds {TOKEN}")
    return problems


def _shape_problems(workflow: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    concurrency = workflow.get("concurrency")
    if not (
        isinstance(concurrency, dict)
        and concurrency.get("group")
        and concurrency.get("cancel-in-progress") is False
    ):
        problems.append("workflow: no single concurrency group with cancel-in-progress: false")
    for name, job in _jobs(workflow).items():
        # A job that holds a secret already had each of these refused by its key allow-list.
        if not _holds_secret(job):
            if "uses" in job:
                problems.append(f"jobs.{name}: calls a reusable workflow")
            if "defaults" in job:
                problems.append(f"jobs.{name}: sets defaults (a shell override)")
            if "continue-on-error" in job:
                problems.append(f"jobs.{name}: continue-on-error")
        token_job = _holds_secret(job, TOKEN)
        for index, step in enumerate(job.get("steps") or []):
            problems.extend(_step_problems(name, index, step, token_job))
    return problems


def _environment_problems(workflow: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    for name, job in _jobs(workflow).items():
        environment = job.get("environment")
        declared = environment.get("name") if isinstance(environment, dict) else environment
        if _holds_secret(job) and declared != ENVIRONMENT:
            problems.append(f"jobs.{name}: holds a secret without environment: {ENVIRONMENT}")
    return problems


def check_deploy_wiring(workflow: Mapping[str, Any], gate_problems: GateCheck) -> list[str]:
    """Every way the workflow breaks the shared-settings wiring or lets a secret out.

    The ONE place the verdict is decided: :func:`main` reports exactly this list and the
    self-test breaks a sample workflow through it, so it cannot be emptied unnoticed.
    """
    gates = [
        problem
        for name, job in _jobs(workflow).items()
        for problem in gate_problems(name, job.get("if"))
    ]
    return [
        *_key_problems(workflow),
        *_structure_problems(workflow),
        *_allow_list_problems(workflow),
        *_secret_problems(workflow),
        *_shape_problems(workflow),
        *_environment_problems(workflow),
        *gates,
    ]


# ---- the self-test ------------------------------------------------------------------------

_GATE = (
    "(github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main') || "
    "(github.event.workflow_run.conclusion == 'success' && "
    "github.event.workflow_run.event == 'push' && "
    "github.event.workflow_run.head_branch == 'main' && "
    "github.event.workflow_run.head_repository.full_name == github.repository)"
)
_AFTER_A_FAILURE = f"!cancelled() && needs.{STAGE_JOB}.result == 'success' && "
_VERIFY_GATE = (
    f"({_AFTER_A_FAILURE}github.event_name == 'workflow_dispatch' && "
    "github.ref == 'refs/heads/main') || "
    f"({_AFTER_A_FAILURE}github.event.workflow_run.conclusion == 'success' && "
    "github.event.workflow_run.event == 'push' && "
    "github.event.workflow_run.head_branch == 'main' && "
    "github.event.workflow_run.head_repository.full_name == github.repository)"
)


def _sample() -> dict[str, Any]:
    """The smallest workflow wired correctly, built from the allow-list itself."""

    def job(name: str, needs: list[str], gate: str = _GATE) -> dict[str, Any]:
        spec: dict[str, Any] = {"if": gate, "runs-on": RUNNER, "environment": ENVIRONMENT}
        if needs:
            spec["needs"] = needs
        spec["steps"] = copy.deepcopy(ALLOWED_STEPS[name])
        return spec

    return {
        "permissions": dict(WORKFLOW_PERMISSIONS),
        "concurrency": {"group": "deploy", "cancel-in-progress": False},
        "jobs": {
            VALIDATE_JOB: job(VALIDATE_JOB, []),
            STAGE_JOB: job(STAGE_JOB, [VALIDATE_JOB]),
            DEPLOY_JOBS[0]: job(DEPLOY_JOBS[0], [STAGE_JOB]),
            DEPLOY_JOBS[1]: job(DEPLOY_JOBS[1], [STAGE_JOB]),
            VERIFY_JOB: job(VERIFY_JOB, [STAGE_JOB, *DEPLOY_JOBS], _VERIFY_GATE),
        },
    }


def _job(w: dict[str, Any], name: str) -> dict[str, Any]:
    job: dict[str, Any] = w["jobs"][name]
    return job


def _step(w: dict[str, Any], name: str, index: int) -> dict[str, Any]:
    step: dict[str, Any] = _job(w, name)["steps"][index]
    return step


_API, _VOICE = DEPLOY_JOBS
_MID = "PERSONA_MID_MODELS"
_HERE = "not the step the allow-list has here"
_OUTSIDE = "a secret outside its allowed site"
_TOOLING = f"Python tooling in a job that holds {TOKEN}"
_KEY = "is not allowed on a job that holds a secret"

#: (name, how the sample is broken, the problems the gate must report). Run every invocation.
FIXTURES: tuple[tuple[str, Callable[[dict[str, Any]], None] | None, list[str]], ...] = (
    ("the correct sample", None, []),
    (
        "an extra step echoing a fragment",
        lambda w: _job(w, STAGE_JOB)["steps"].append(
            {"env": {_MID: _secret(_MID)}, "run": f"echo ${{{_MID}:0:6}}"}
        ),
        [
            f"jobs.{STAGE_JOB}.steps[3]: {_HERE}",
            f"jobs.{STAGE_JOB}.steps[3]: reads FLY_API_TOKEN or a PERSONA_ setting in its script",
        ],
    ),
    (
        "a token step that prints part of the token",
        lambda w: _step(w, STAGE_JOB, 2).update(run="flyctl version\necho ${FLY_API_TOKEN:0:8}\n"),
        [
            f"jobs.{STAGE_JOB}.steps[2]: {_HERE}",
            f"jobs.{STAGE_JOB}.steps[2]: reads FLY_API_TOKEN or a PERSONA_ setting in its script",
        ],
    ),
    (
        "toJSON(secrets)",
        lambda w: _step(w, _API, 3).update(run="echo '${{ toJSON(secrets) }}' > f"),
        [f"jobs.{_API}.steps[3]: {_HERE}", f"jobs.{_API}.steps[3].run: uses toJSON(secrets)"],
    ),
    (
        "bracket access",
        lambda w: _step(w, _API, 2)["env"].update(X="${{ secrets['FLY_API_TOKEN'] }}"),
        [f"jobs.{_API}.steps[2]: {_HERE}", f"jobs.{_API}.steps[2].env.X: reads secrets by bracket"],
    ),
    (
        "a secret in job-level env",
        lambda w: _job(w, _VOICE).update(env={TOKEN: _secret(TOKEN)}),
        [f"jobs.{_VOICE}: key env {_KEY}", f"jobs.{_VOICE}.env.{TOKEN}: {_OUTSIDE}"],
    ),
    (
        "a secret in workflow-level env",
        lambda w: w.update(env={_MID: _secret(_MID)}),
        ["workflow: key env is not allowed", f"env.{_MID}: {_OUTSIDE}"],
    ),
    (
        "shell: bash -x {0}",
        lambda w: _step(w, STAGE_JOB, 2).update(shell="bash -x {0}"),
        [f"jobs.{STAGE_JOB}.steps[2]: {_HERE}", f"jobs.{STAGE_JOB}.steps[2]: overrides the shell"],
    ),
    (
        "continue-on-error",
        lambda w: _job(w, _API).update({"continue-on-error": True}),
        [f"jobs.{_API}: key continue-on-error {_KEY}"],
    ),
    (
        "if: always()",
        lambda w: _job(w, _VOICE).update({"if": "always()"}),
        [f"{_VOICE}: expected exactly two paths, got 1"],
    ),
    ("a job with no if", lambda w: _job(w, _API).pop("if"), [f"{_API} has no gate at all"]),
    (
        "the validate job holding the Fly token",
        lambda w: _step(w, VALIDATE_JOB, 3)["env"].update({TOKEN: _secret(TOKEN)}),
        [
            f"jobs.{VALIDATE_JOB}.steps[3]: {_HERE}",
            f"jobs.{VALIDATE_JOB}.steps[3].env.{TOKEN}: {_OUTSIDE}",
            f"jobs.{VALIDATE_JOB}.steps[1]: {_TOOLING}",
            f"jobs.{VALIDATE_JOB}.steps[2]: {_TOOLING}",
            f"jobs.{VALIDATE_JOB}.steps[3]: {_TOOLING}",
        ],
    ),
    (
        "uv in the stage job, before the stage step",
        lambda w: _job(w, STAGE_JOB)["steps"].insert(2, {"run": "uv sync"}),
        [f"jobs.{STAGE_JOB}.steps[2]: {_HERE}", f"jobs.{STAGE_JOB}.steps[2]: {_TOOLING}"],
    ),
    (
        "setup-python in a deploy job",
        lambda w: _job(w, _VOICE)["steps"].insert(1, {"uses": "actions/setup-python@v5"}),
        [f"jobs.{_VOICE}.steps[1]: {_HERE}", f"jobs.{_VOICE}.steps[1]: {_TOOLING}"],
    ),
    (
        "|| true",
        lambda w: _step(w, _API, 2).update(run="flyctl deploy --app x || true"),
        [f"jobs.{_API}.steps[2]: {_HERE}", f"jobs.{_API}.steps[2]: swallows a failure"],
    ),
    (
        "set +e",
        lambda w: _step(w, _API, 3).update(run="set +e\ncurl -fsS https://x.example\n"),
        [f"jobs.{_API}.steps[3]: {_HERE}", f"jobs.{_API}.steps[3]: swallows a failure"],
    ),
    (
        "trap",
        lambda w: _step(w, VERIFY_JOB, 2).update(run="trap 'exit 0' ERR\nflyctl version\n"),
        [f"jobs.{VERIFY_JOB}.steps[2]: {_HERE}", f"jobs.{VERIFY_JOB}.steps[2]: swallows a failure"],
    ),
    (
        "set -x",
        lambda w: _step(w, VALIDATE_JOB, 3).update(run="set -x\nuv run python x.py\n"),
        [
            f"jobs.{VALIDATE_JOB}.steps[3]: {_HERE}",
            f"jobs.{VALIDATE_JOB}.steps[3]: turns on command tracing",
        ],
    ),
    (
        "a reusable workflow inheriting the secrets",
        lambda w: w["jobs"].update(
            reuse={
                "if": _GATE,
                "uses": "org/repo/.github/workflows/x.yml@main",
                "secrets": "inherit",
            }
        ),
        [
            "jobs.reuse.secrets: passes secrets on (secrets:)",
            "jobs.reuse: calls a reusable workflow",
        ],
    ),
    (
        "a secret-holding job outside the production Environment",
        lambda w: _job(w, _API).pop("environment"),
        [f"jobs.{_API}: holds a secret without environment: {ENVIRONMENT}"],
    ),
    (
        "a deploy that does not wait for the stage",
        lambda w: _job(w, _VOICE).pop("needs"),
        [f"jobs.{_VOICE}: does not need {STAGE_JOB}"],
    ),
    (
        "a setting mapped to another secret",
        lambda w: _step(w, STAGE_JOB, 2)["env"].update({_MID: _secret("PERSONA_SMALL_MODELS")}),
        [
            f"jobs.{STAGE_JOB}.steps[2]: {_HERE}",
            f"jobs.{STAGE_JOB}.steps[2].env.{_MID}: {_OUTSIDE}",
        ],
    ),
    (
        "container: python:3.12 on the stage job",
        lambda w: _job(w, STAGE_JOB).update(container="python:3.12"),
        [f"jobs.{STAGE_JOB}: key container {_KEY}"],
    ),
    (
        "services on a deploy job",
        lambda w: _job(w, _API).update(services={"cache": {"image": "redis:7"}}),
        [f"jobs.{_API}: key services {_KEY}"],
    ),
    (
        "a job-level BASH_ENV",
        lambda w: _job(w, STAGE_JOB).update(env={"BASH_ENV": "/tmp/hook.sh"}),
        [f"jobs.{STAGE_JOB}: key env {_KEY}"],
    ),
    (
        "a workflow-level LD_PRELOAD",
        lambda w: w.update(env={"LD_PRELOAD": "/tmp/hook.so"}),
        ["workflow: key env is not allowed"],
    ),
    (
        "runs-on: self-hosted",
        lambda w: _job(w, _VOICE).update({"runs-on": "self-hosted"}),
        [f"jobs.{_VOICE}: runs-on must be {RUNNER}"],
    ),
    (
        "job-level permissions on a secret-holding job",
        lambda w: _job(w, VERIFY_JOB).update(
            permissions={"contents": "write", "id-token": "write"}
        ),
        [f"jobs.{VERIFY_JOB}: key permissions {_KEY}"],
    ),
    (
        "job-level permissions on any other job",
        lambda w: w["jobs"].update(
            other={
                "if": _GATE,
                "runs-on": RUNNER,
                "permissions": {"contents": "write"},
                "steps": [],
            }
        ),
        ["jobs.other: sets its own permissions"],
    ),
    (
        "a per-job concurrency group",
        lambda w: _job(w, _API).update(concurrency={"group": "x", "cancel-in-progress": True}),
        [f"jobs.{_API}: key concurrency {_KEY}"],
    ),
    (
        "wider workflow permissions",
        lambda w: w.update(permissions={"contents": "write"}),
        ["workflow: permissions must be exactly contents: read"],
    ),
    (
        "no workflow-level concurrency",
        lambda w: w.pop("concurrency"),
        ["workflow: no single concurrency group with cancel-in-progress: false"],
    ),
)


def selftest(gate_problems: GateCheck) -> int:
    """Fail loudly if the gate stops catching what it was built to catch."""
    broken = 0
    for label, breaker, expected in FIXTURES:
        sample = _sample()
        if breaker is not None:
            breaker(sample)
        got = check_deploy_wiring(sample, gate_problems)
        if got != expected:
            print(f"   SELFTEST: {label}: expected {expected}, got {got}")
            broken += 1
    return broken


def load_fork_gate() -> ModuleType:
    """The fork test, loaded for its ``gate_problems``, so the gate has one definition."""
    spec = importlib.util.spec_from_file_location("test_deploy_fork_gate", FORK_GATE_TEST)
    if spec is None or spec.loader is None:
        raise ImportError(str(FORK_GATE_TEST))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    try:
        import yaml
        from persona_runtime.chain_report import UNIFIED_ENV_NAMES

        gate_problems: GateCheck = load_fork_gate().gate_problems
    except Exception as exc:  # noqa: BLE001 - a gate that cannot load what it checks must not pass
        print(f"cannot load the shared settings, PyYAML or the fork gate: {exc!r}")
        return 1
    if tuple(UNIFIED_ENV_NAMES) != SHARED:
        print("This gate's list of shared settings differs from UNIFIED_ENV_NAMES.")
        return 1
    if broken := selftest(gate_problems):
        print(f"The deploy wiring gate is not working: {broken} of {len(FIXTURES)} fixtures wrong.")
        return 1
    if not DEPLOY_WORKFLOW.is_file():
        print(f"{DEPLOY_WORKFLOW.relative_to(ROOT).as_posix()} is missing.")
        return 1
    workflow = yaml.safe_load(DEPLOY_WORKFLOW.read_text(encoding="utf-8"))
    if not isinstance(workflow, dict) or not _jobs(workflow):
        print("The deploy workflow has no jobs; the gate would pass on nothing.")
        return 1
    problems = check_deploy_wiring(workflow, gate_problems)
    print(
        f"Self-test {len(FIXTURES)} of {len(FIXTURES)} fixtures right. Checked "
        f"{len(_jobs(workflow))} jobs of .github/workflows/deploy.yml against step allow-lists "
        f"for {len(ALLOWED_STEPS)} secret-holding jobs."
    )
    if problems:
        print(f"\n{len(problems)} deploy wiring problem(s):\n")
        for problem in problems:
            print(f"   WIRING  {problem}")
        print(
            "\nThe api and voice must be handed the same seven settings from one source, and no\n"
            "secret may reach anything else; see setup step 4 in the header of deploy.yml. A\n"
            "deliberate change to a secret-holding job's steps is also a change to ALLOWED_STEPS."
        )
        return 1
    print("Deploy wiring gate clean.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
