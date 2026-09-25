"""A pull request from a fork can never trigger a production deploy.

``.github/workflows/deploy.yml`` runs on ``workflow_run`` of CI, in the base repository,
with the production secrets, and checks out ``github.event.workflow_run.head_sha``. CI also
runs on ``pull_request``, with the pull request's own ``ci.yml``. So a pull request from a
fork's ``main`` branch produced a green CI run whose ``head_branch`` is ``main``, and a gate
that only checked the conclusion and the branch name deployed the fork's commit with the
org-scoped ``FLY_API_TOKEN``.

These tests read the real workflow and require every job's ``if`` to be exactly two paths:

* a CI run that succeeded for a PUSH to ``main`` in THIS repository, or
* a manual run from ``refs/heads/main``.

The expression is parsed into its ``||`` paths and each path's ``&&`` conditions, so a
condition that is present but reachable around (``A || event == 'push'``) fails too.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

_WORKFLOW = Path(__file__).resolve().parents[4] / ".github" / "workflows" / "deploy.yml"

_CI_PUSH_TO_MAIN_HERE = frozenset(
    {
        "github.event.workflow_run.conclusion == 'success'",
        "github.event.workflow_run.event == 'push'",
        "github.event.workflow_run.head_branch == 'main'",
        "github.event.workflow_run.head_repository.full_name == github.repository",
    }
)
_MANUAL_RUN_FROM_MAIN = frozenset(
    {
        "github.event_name == 'workflow_dispatch'",
        "github.ref == 'refs/heads/main'",
    }
)


def _split_top_level(expr: str, operator: str) -> list[str]:
    """Split ``expr`` on ``operator`` where it is not inside parentheses or quotes."""
    parts: list[str] = []
    depth, quoted, start, i = 0, False, 0, 0
    while i < len(expr):
        char = expr[i]
        if char == "'":
            quoted = not quoted
        elif not quoted and char == "(":
            depth += 1
        elif not quoted and char == ")":
            depth -= 1
        elif not quoted and depth == 0 and expr.startswith(operator, i):
            parts.append(expr[start:i])
            start = i + len(operator)
            i = start
            continue
        i += 1
    parts.append(expr[start:])
    return [_strip_parens(" ".join(part.split())) for part in parts]


def _strip_parens(expr: str) -> str:
    """Remove parentheses that wrap the whole expression, and only those."""
    while expr.startswith("(") and expr.endswith(")"):
        depth, quoted = 0, False
        for i, char in enumerate(expr):
            if char == "'":
                quoted = not quoted
            elif not quoted:
                depth += (char == "(") - (char == ")")
            if depth == 0 and not quoted and i < len(expr) - 1:
                return expr
        expr = expr[1:-1].strip()
    return expr


def paths(condition: str) -> list[frozenset[str]]:
    """Each ``||`` path of a job condition, as the set of its ``&&`` conditions."""
    return [
        frozenset(_split_top_level(path, "&&"))
        for path in _split_top_level(" ".join(condition.split()), "||")
    ]


def _load() -> dict[Any, Any]:
    loaded = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _jobs() -> dict[str, dict[str, Any]]:
    jobs = _load()["jobs"]
    assert jobs, "a gate over no jobs proves nothing"
    return jobs


def test_the_workflow_has_deploy_jobs_to_check() -> None:
    assert {"deploy-api", "deploy-voice", "deploy-livekit"} <= set(_jobs())


#: Conditions ONE job may carry on BOTH paths on top of the canonical ones. Only the job that
#: must report a split deploy (the api deployed, voice failed) runs after a failed
#: dependency, and it still requires the stage to have succeeded plus every canonical
#: condition. Any other job carrying these fails.
_EXTRA_ON_EVERY_PATH: dict[str, frozenset[str]] = {
    "verify-model-chains": frozenset(
        {"!cancelled()", "needs.stage-model-chains.result == 'success'"}
    ),
}


def gate_problems(job: str, condition: object) -> list[str]:
    """Every way ``job``'s ``if`` departs from the two allowed paths; empty when it is right.

    Shared with ``scripts/check_deploy_wiring.py``, so the gate has one definition.
    """
    if not isinstance(condition, str):
        return [f"{job} has no gate at all"]
    found = paths(condition)
    # Every split condition must be a single comparison. An ``||`` inside a path's own
    # parentheses would otherwise glue into one "condition" and reopen a second way in:
    # ``(A && B && C && D && true || A && C)`` is the pre-fix gate wearing all four checks.
    glued = sorted(c for path in found for c in path if "||" in c or "&&" in c)
    if glued:
        return [f"{job}: conditions that hide another operator: {glued}"]
    extra = _EXTRA_ON_EVERY_PATH.get(job, frozenset())
    if not all(path >= extra for path in found):
        return [f"{job}: every path must also require {sorted(extra)}"]
    found = [path - extra for path in found]
    if len(found) != 2:
        return [f"{job}: expected exactly two paths, got {len(found)}"]
    manual = [p for p in found if "github.event_name == 'workflow_dispatch'" in p]
    automatic = [p for p in found if p not in manual]
    problems: list[str] = []
    if manual != [_MANUAL_RUN_FROM_MAIN]:
        problems.append(f"{job}: manual path is {[sorted(p) for p in manual]}")
    if automatic != [_CI_PUSH_TO_MAIN_HERE]:
        problems.append(f"{job}: CI path is {[sorted(p) for p in automatic]}")
    return problems


@pytest.mark.parametrize("job", sorted(_jobs()))
def test_every_job_runs_only_for_a_push_to_main_here_or_a_manual_run_from_main(job: str) -> None:
    assert gate_problems(job, _jobs()[job].get("if")) == []


def test_only_the_verify_job_may_run_after_a_failed_dependency() -> None:
    """The one allowance is scoped: the same condition on a deploy job is refused."""
    assert set(_EXTRA_ON_EVERY_PATH) == {"verify-model-chains"}
    extra = _EXTRA_ON_EVERY_PATH["verify-model-chains"]
    after_failure = (
        "(!cancelled() && needs.stage-model-chains.result == 'success' && "
        "github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main') || "
        "(!cancelled() && needs.stage-model-chains.result == 'success' && "
        + " && ".join(sorted(_CI_PUSH_TO_MAIN_HERE))
        + ")"
    )
    assert gate_problems("verify-model-chains", after_failure) == []
    assert gate_problems("deploy-api", after_failure) == [
        f"deploy-api: manual path is {[sorted(_MANUAL_RUN_FROM_MAIN | extra)]}",
        f"deploy-api: CI path is {[sorted(_CI_PUSH_TO_MAIN_HERE | extra)]}",
    ]


def test_the_workflow_token_can_only_read() -> None:
    assert _load().get("permissions") == {"contents": "read"}


def _steps() -> list[dict[str, Any]]:
    steps = [step for job in _jobs().values() for step in job.get("steps") or []]
    assert steps, "a check over no steps proves nothing"
    return steps


def test_every_action_is_pinned_to_a_full_commit_sha() -> None:
    """A moving ref such as ``@master`` or a tag such as ``@v4`` runs whatever it points at
    on the day of the deploy, with the production secrets in the job. Every action,
    ``actions/checkout`` included, is pinned to a full 40-hex commit SHA."""
    uses = [str(step["uses"]) for step in _steps() if "uses" in step]
    assert uses, "the workflow uses no actions; the check would pass on nothing"
    unpinned = [ref for ref in uses if re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", ref) is None]
    assert unpinned == []


def test_no_checkout_leaves_its_token_behind_for_later_steps() -> None:
    checkouts = [
        step for step in _steps() if str(step.get("uses", "")).startswith("actions/checkout@")
    ]
    assert len(checkouts) >= 3
    assert [step.get("with", {}).get("persist-credentials") for step in checkouts] == [False] * len(
        checkouts
    )


def test_the_path_parser_sees_through_a_condition_that_can_be_reached_around() -> None:
    """The parser this gate rests on: a required condition on its own ``||`` path does not
    protect the other path."""
    assert paths("a == 'x' || (b == 'y' && c == 'z')") == [
        frozenset({"a == 'x'"}),
        frozenset({"b == 'y'", "c == 'z'"}),
    ]
    assert paths("(a == 'x' && b == 'y')") == [frozenset({"a == 'x'", "b == 'y'"})]
    assert paths("(a == '(') || (b == ')')") == [frozenset({"a == '('"}), frozenset({"b == ')'"})]
