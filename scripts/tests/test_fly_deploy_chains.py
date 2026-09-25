"""The standard-library-only script that stages and verifies the shared settings on Fly.

It is the only process that holds the Fly token, so it may import nothing outside the
standard library, and it must never print a value or a digest. Every test here drives it
against a real subprocess: a small stand-in for flyctl that records what it was handed
(arguments, stdin, environment) and, in the worst case a real flyctl could manage, echoes
its stdin to both of its output streams and writes the metrics warning flyctl really
writes to stderr. File-descriptor capture then proves none of that reaches this job's log.

The flyctl contract the verify stand-in reproduces is CONFIRMED, not assumed: the
orchestrator ran real flyctl v0.4.108 on 2026-09-25 (``flyctl secrets list -a <app>
--json``) and saw stdout carry a JSON list of objects with exactly the keys ``digest``,
``name`` and ``status``, every status ``Deployed``, every digest 16 hex characters, and
warnings such as ``Warning: Metrics token unavailable: ...`` on stderr only.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from persona_runtime.chain_report import UNIFIED_ENV_NAMES

if TYPE_CHECKING:
    from types import ModuleType

_SCRIPT = Path(__file__).resolve().parents[1] / "fly_deploy_chains.py"
_APPS = ["open-persona-api", "open-persona-voice"]
_TOKEN = "FlyV1 " + "fm2_" + "t0k3n" * 8
_WARNING = "Warning: Metrics token unavailable: context canceled"

_VALID = {
    "PERSONA_FRONTIER_MODELS": " openrouter/anthropic/claude-x ,  openrouter/z-ai/glm-y ",
    "PERSONA_MID_MODELS": "openrouter / z-ai/glm-flash",
    "PERSONA_SMALL_MODELS": "groq/some-small-model",
    "PERSONA_FREE_FRONTIER_MODELS": "openrouter/vendor/frontier:free",
    "PERSONA_FREE_MID_MODELS": "cloudflare/@cf/meta/llama-x",
    "PERSONA_FREE_SMALL_MODELS": "openrouter/vendor/small:free",
    "PERSONA_OPENROUTER_SUBSCRIPTION_MODE": "paid",
}
_CANONICAL = {
    **_VALID,
    "PERSONA_FRONTIER_MODELS": "openrouter/anthropic/claude-x,openrouter/z-ai/glm-y",
    "PERSONA_MID_MODELS": "openrouter/z-ai/glm-flash",
}

_STAND_IN = r"""
import json, os, sys
config = json.load(open(sys.argv[1], encoding="utf-8"))
args = sys.argv[2:]
app = args[args.index("-a") + 1]
stdin = sys.stdin.read() if "import" in args else ""
with open(config["log"], "a", encoding="utf-8") as log:
    log.write(json.dumps({"args": args, "stdin": stdin, "env": dict(os.environ)}) + "\n")
sys.stderr.write(config["warning"] + "\n" + stdin)
if "list" in args:
    sys.stdout.write(config["listings"].get(app, "[]"))
else:
    sys.stdout.write(stdin)
sys.exit(config["exit"].get(app, 0))
"""


@pytest.fixture(scope="module")
def fly() -> ModuleType:
    spec = importlib.util.spec_from_file_location("fly_deploy_chains", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["fly_deploy_chains"] = module
    spec.loader.exec_module(module)
    return module


class StandIn:
    """A real flyctl-shaped subprocess, configured per test, recording every call."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        exit_codes: dict[str, int] | None = None,
        listings: dict[str, str] | None = None,
    ) -> None:
        self.log = tmp_path / "calls.jsonl"
        script = tmp_path / "flyctl_stand_in.py"
        script.write_text(_STAND_IN, encoding="utf-8")
        config = tmp_path / "config.json"
        config.write_text(
            json.dumps(
                {
                    "log": str(self.log),
                    "warning": _WARNING,
                    "exit": exit_codes or {},
                    "listings": listings or {},
                }
            ),
            encoding="utf-8",
        )
        self.argv = (sys.executable, str(script), str(config))

    def calls(self) -> list[dict[str, Any]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]


def _env(**overrides: str | None) -> dict[str, str]:
    """A process environment: what the step gives the script, plus what a runner has."""
    import os

    base = {k: v for k, v in os.environ.items() if k in ("PATH", "SYSTEMROOT", "HOME", "TEMP")}
    env = {**base, **_VALID, "FLY_API_TOKEN": _TOKEN, "UNRELATED_SECRET": "must-not-leak"}
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def _output(capfd: pytest.CaptureFixture[str]) -> list[str]:
    captured = capfd.readouterr()
    assert captured.err == ""
    return captured.out.splitlines()


# ---- the process that holds the token ---------------------------------------------------


def test_the_script_imports_the_standard_library_only() -> None:
    tree = ast.parse(_SCRIPT.read_text(encoding="utf-8"))
    imported = {
        name.split(".")[0]
        for node in ast.walk(tree)
        for name in (
            [alias.name for alias in node.names]
            if isinstance(node, ast.Import)
            else [node.module or ""]
            if isinstance(node, ast.ImportFrom)
            else []
        )
    }
    assert imported, "a check over no imports proves nothing"
    assert sorted(imported - set(sys.stdlib_module_names) - {"__future__"}) == []


def test_its_copy_of_the_seven_names_is_the_runtimes(fly: ModuleType) -> None:
    assert fly.SHARED_NAMES == UNIFIED_ENV_NAMES


# ---- stage -------------------------------------------------------------------------------


def test_stage_hands_every_app_the_same_canonical_seven_on_stdin_and_prints_no_value(
    fly: ModuleType, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    stand_in = StandIn(tmp_path)
    code = fly.stage(_APPS, _env(), stand_in.argv)
    lines = _output(capfd)
    assert code == 0
    assert lines == [
        "Staged 7 settings on open-persona-api.",
        "Staged 7 settings on open-persona-voice.",
        "Each app applies them on its next deploy.",
    ]
    payload = "".join(f"{name}={_CANONICAL[name]}\n" for name in UNIFIED_ENV_NAMES)
    calls = stand_in.calls()
    assert [(c["args"], c["stdin"]) for c in calls] == [
        (["secrets", "import", "--stage", "-a", app], payload) for app in _APPS
    ]
    for call in calls:
        assert call["env"]["FLY_API_TOKEN"] == _TOKEN
        assert set(call["env"]) - {"FLY_API_TOKEN"} <= set(fly._FLYCTL_ENV)
        assert not set(UNIFIED_ENV_NAMES) & set(call["env"])
    for secret in (*_VALID.values(), *_CANONICAL.values(), _TOKEN, _WARNING):
        assert secret.strip() not in "\n".join(lines)


_REFUSED = [
    pytest.param("PERSONA_MID_MODELS", None, id="missing"),
    pytest.param("PERSONA_MID_MODELS", "", id="empty"),
    pytest.param("PERSONA_MID_MODELS", "openrouter/z-ai/glm\nFLY_API_TOKEN=x", id="newline"),
    pytest.param("PERSONA_MID_MODELS", "openrouter/z-ai/glm=x", id="equals"),
    pytest.param("PERSONA_MID_MODELS", "openrouter/https://h.example/x", id="url"),
    pytest.param("PERSONA_MID_MODELS", "openrouter/u:p@h.example", id="at-sign"),
    pytest.param("PERSONA_MID_MODELS", "no-slash", id="no-slash"),
    pytest.param("PERSONA_OPENROUTER_SUBSCRIPTION_MODE", "Paid", id="mode-capitalised"),
    pytest.param("PERSONA_OPENROUTER_SUBSCRIPTION_MODE", None, id="mode-missing"),
]


@pytest.mark.parametrize(("name", "value"), _REFUSED)
def test_stage_refuses_before_calling_flyctl_when_a_value_fails_the_recheck(
    fly: ModuleType,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
    name: str,
    value: str | None,
) -> None:
    stand_in = StandIn(tmp_path)
    code = fly.stage(_APPS, _env(**{name: value}), stand_in.argv)
    lines = _output(capfd)
    assert code == 1
    assert stand_in.calls() == []
    assert lines == [
        f"Refusing to stage: {name} failed the re-check. Nothing has been written to any app."
    ]


def test_stage_refuses_without_a_token(
    fly: ModuleType, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    stand_in = StandIn(tmp_path)
    code = fly.stage(_APPS, _env(FLY_API_TOKEN=None), stand_in.argv)
    assert code == 1
    assert stand_in.calls() == []
    assert _output(capfd) == [
        "Refusing to stage: FLY_API_TOKEN is not set. Nothing has been written to any app."
    ]


def test_a_flyctl_failure_on_one_app_stops_the_run_and_its_output_stays_out_of_the_log(
    fly: ModuleType, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    stand_in = StandIn(tmp_path, exit_codes={"open-persona-voice": 3})
    code = fly.stage(_APPS, _env(), stand_in.argv)
    assert code == 1
    assert _output(capfd) == [
        "Staged 7 settings on open-persona-api.",
        "flyctl secrets import failed on open-persona-voice (exit 3). Its output is withheld "
        "because it can carry values. Check that FLY_API_TOKEN may write secrets on that app.",
    ]


# ---- verify ------------------------------------------------------------------------------
#
# The contract these stand-ins reproduce, confirmed by the orchestrator against real
# flyctl v0.4.108 on 2026-09-25 (`flyctl secrets list -a <app> --json`): stdout is a JSON
# list of objects with exactly the keys `digest`, `name` and `status`; every status seen
# was `Deployed` (a staged value reads `Staged` in the table view); digests are 16 hex
# characters; warnings such as `Warning: Metrics token unavailable: ...` go to stderr.

_DIGESTS = {name: f"{i:x}{i + 8:x}" * 8 for i, name in enumerate(UNIFIED_ENV_NAMES, start=1)}


def _listing(digests: dict[str, str], status: str = "Deployed") -> str:
    """A ``secrets list --json`` answer in the confirmed shape: 16-hex digests, three keys."""
    rows = [{"name": n, "digest": d, "status": status} for n, d in digests.items()]
    rows.append({"name": "PERSONA_UNRELATED_KEY", "digest": "f" * 16, "status": "Deployed"})
    return json.dumps(rows)


def _no_hex_from(digests: dict[str, str], lines: list[str]) -> None:
    printed = "\n".join(lines)
    for digest in digests.values():
        for start in range(0, len(digest) - 3):
            assert digest[start : start + 4] not in printed


def test_verify_reports_match_per_setting_and_app_and_never_a_digest(
    fly: ModuleType, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """The stand-in answers in the shape confirmed against real flyctl v0.4.108 on
    2026-09-25: a JSON list of objects with exactly ``digest``, ``name`` and ``status``,
    status ``Deployed``, 16-hex digests, the metrics warning on stderr."""
    assert all(len(d) == 16 and int(d, 16) >= 0 for d in _DIGESTS.values())
    stand_in = StandIn(tmp_path, listings={app: _listing(_DIGESTS) for app in _APPS})
    code = fly.verify(_APPS, _env(), stand_in.argv)
    lines = _output(capfd)
    assert code == 0
    assert [c["args"] for c in stand_in.calls()] == [
        ["secrets", "list", "-a", app, "--json"] for app in _APPS
    ]
    assert lines == [
        *(
            f"{name}: open-persona-api match, open-persona-voice match"
            for name in UNIFIED_ENV_NAMES
        ),
        "All 7 settings match and are deployed on 2 apps.",
    ]
    _no_hex_from(_DIGESTS, lines)
    assert _WARNING not in "\n".join(lines)


def test_verify_compares_the_whole_digest_not_a_prefix(
    fly: ModuleType, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """Two values whose digests share their first 15 characters are still different."""
    voice = dict(_DIGESTS)
    voice["PERSONA_SMALL_MODELS"] = _DIGESTS["PERSONA_SMALL_MODELS"][:15] + "0"
    assert voice["PERSONA_SMALL_MODELS"] != _DIGESTS["PERSONA_SMALL_MODELS"]
    stand_in = StandIn(
        tmp_path,
        listings={"open-persona-api": _listing(_DIGESTS), "open-persona-voice": _listing(voice)},
    )
    assert fly.verify(_APPS, _env(), stand_in.argv) == 1
    lines = _output(capfd)
    assert lines[2] == "PERSONA_SMALL_MODELS: open-persona-api differs, open-persona-voice differs"
    assert lines[-1] == "1 of 7 settings do not match or are not deployed on 2 apps."


def test_verify_names_differs_missing_and_not_deployed_without_a_digest(
    fly: ModuleType, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    voice = dict(_DIGESTS)
    voice["PERSONA_MID_MODELS"] = "e" * 16
    del voice["PERSONA_SMALL_MODELS"]
    api_rows = json.loads(_listing(_DIGESTS))
    for row in api_rows:
        if row["name"] == "PERSONA_FREE_MID_MODELS":
            row["status"] = "Staged"
    stand_in = StandIn(
        tmp_path,
        listings={"open-persona-api": json.dumps(api_rows), "open-persona-voice": _listing(voice)},
    )
    code = fly.verify(_APPS, _env(), stand_in.argv)
    lines = _output(capfd)
    assert code == 1
    both_match = "open-persona-api match, open-persona-voice match"
    assert lines == [
        f"PERSONA_FRONTIER_MODELS: {both_match}",
        "PERSONA_MID_MODELS: open-persona-api differs, open-persona-voice differs",
        "PERSONA_SMALL_MODELS: open-persona-api differs, open-persona-voice missing",
        f"PERSONA_FREE_FRONTIER_MODELS: {both_match}",
        "PERSONA_FREE_MID_MODELS: open-persona-api not deployed, open-persona-voice match",
        f"PERSONA_FREE_SMALL_MODELS: {both_match}",
        f"PERSONA_OPENROUTER_SUBSCRIPTION_MODE: {both_match}",
        "3 of 7 settings do not match or are not deployed on 2 apps.",
    ]
    _no_hex_from({**_DIGESTS, "x": "e" * 16}, lines)


def test_verify_parses_stdout_only_when_stderr_carries_a_warning(
    fly: ModuleType, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """flyctl writes ``Warning: Metrics token unavailable`` to stderr next to the JSON on
    stdout; merging the two would break the parse, printing stderr would leak its text."""
    stand_in = StandIn(tmp_path, listings={app: _listing(_DIGESTS) for app in _APPS})
    assert fly.verify(_APPS, _env(), stand_in.argv) == 0
    captured = capfd.readouterr()
    assert _WARNING not in captured.out
    assert captured.err == ""


def test_verify_fails_loudly_when_flyctl_fails_or_answers_something_unexpected(
    fly: ModuleType, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    failing = StandIn(tmp_path, exit_codes={"open-persona-api": 1})
    assert fly.verify(_APPS, _env(), failing.argv) == 1
    assert _output(capfd) == ["flyctl secrets list failed on open-persona-api (exit 1)."]
    garbled = StandIn(tmp_path, listings={"open-persona-api": "not json"})
    assert fly.verify(_APPS, _env(), garbled.argv) == 1
    assert _output(capfd) == [
        "flyctl secrets list on open-persona-api did not answer with the expected JSON."
    ]


# ---- never a value, even when something breaks -------------------------------------------


@pytest.mark.parametrize("command", ["stage", "verify"])
def test_a_value_bearing_failure_names_only_its_type(
    fly: ModuleType,
    capfd: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    def _explode(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError(f"{_TOKEN} {_VALID['PERSONA_MID_MODELS']} {'ab' * 8}")

    monkeypatch.setattr(fly.subprocess, "run", _explode)
    assert fly.main([command, *_APPS], env=_env()) == 1
    assert _output(capfd) == [
        f"fly_deploy_chains {command} failed (RuntimeError). It prints no values."
    ]
