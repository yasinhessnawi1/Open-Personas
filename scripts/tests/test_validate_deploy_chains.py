"""The deploy's validator for the seven settings every hosted process shares (R9-213 part 2).

The model chains and the OpenRouter subscription mode come from the seven secrets of the
``production`` Environment. This script checks them with the runtime's own parser, in a
step that holds the values but not the Fly token, before anything is written to either Fly
app. Every output is asserted whole, because the one thing it must never do is print a
value: GitHub masks a secret only as a complete string, so a fragment of one would reach
the Actions log in clear. Fake credentials are assembled at runtime.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from persona_runtime.chain_report import UNIFIED_ENV_NAMES

if TYPE_CHECKING:
    from types import ModuleType

_SCRIPT = Path(__file__).resolve().parents[1] / "validate_deploy_chains.py"
_FAKE_KEY = "sk-" + "or-v1-" + "0a1b2c3d" * 8

_SEVEN = (
    "PERSONA_FRONTIER_MODELS",
    "PERSONA_MID_MODELS",
    "PERSONA_SMALL_MODELS",
    "PERSONA_FREE_FRONTIER_MODELS",
    "PERSONA_FREE_MID_MODELS",
    "PERSONA_FREE_SMALL_MODELS",
    "PERSONA_OPENROUTER_SUBSCRIPTION_MODE",
)

_VALID = {
    "PERSONA_FRONTIER_MODELS": "openrouter/anthropic/claude-x,openrouter/z-ai/glm-y",
    "PERSONA_MID_MODELS": "openrouter/z-ai/glm-flash",
    "PERSONA_SMALL_MODELS": "groq/some-small-model",
    "PERSONA_FREE_FRONTIER_MODELS": "openrouter/vendor/frontier:free",
    "PERSONA_FREE_MID_MODELS": "openrouter/vendor/mid:free",
    "PERSONA_FREE_SMALL_MODELS": "openrouter/vendor/small:free",
    "PERSONA_OPENROUTER_SUBSCRIPTION_MODE": "paid",
}

_NOTHING_WRITTEN = "Nothing has been written to any app."


@pytest.fixture(scope="module")
def vdc() -> ModuleType:
    spec = importlib.util.spec_from_file_location("validate_deploy_chains", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["validate_deploy_chains"] = module
    spec.loader.exec_module(module)
    return module


def _lines(**states: str) -> list[str]:
    return [f"{name}: {states.get(name, 'ok')}" for name in _SEVEN]


def _run(
    vdc: ModuleType, capsys: pytest.CaptureFixture[str], env: dict[str, str]
) -> tuple[int, list[str]]:
    code = vdc.main(env)
    out = capsys.readouterr()
    assert out.err == ""
    return code, out.out.splitlines()


def test_seven_valid_settings_each_read_ok_and_nothing_is_written(
    vdc: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    code, lines = _run(vdc, capsys, _VALID)
    assert code == 0
    assert lines == [*_lines(), f"All 7 settings are valid. {_NOTHING_WRITTEN}"]


def test_the_validator_checks_exactly_the_seven_names_the_runtime_declares(
    vdc: ModuleType,
) -> None:
    assert _SEVEN == UNIFIED_ENV_NAMES
    assert [name for name, _ in vdc.check(_VALID)] == list(UNIFIED_ENV_NAMES)


_PROBLEMS = [
    pytest.param("PERSONA_MID_MODELS", None, "missing", id="chain-absent"),
    pytest.param("PERSONA_MID_MODELS", "", "missing", id="chain-empty"),
    pytest.param("PERSONA_MID_MODELS", "   ", "missing", id="chain-blank"),
    pytest.param(
        "PERSONA_MID_MODELS",
        f"openrouter/z-ai/glm-4.6\nPERSONA_OPENROUTER_API_KEY={_FAKE_KEY}",
        "withheld (an entry is not plainly a model slug)",
        id="chain-with-a-newline",
    ),
    pytest.param(
        "PERSONA_SMALL_MODELS",
        f"openai/{_FAKE_KEY}",
        "withheld (an entry is not plainly a model slug)",
        id="key-as-model",
    ),
    pytest.param(
        "PERSONA_FREE_MID_MODELS", _FAKE_KEY, "malformed (not a model list)", id="bare-key"
    ),
    pytest.param(
        "PERSONA_FREE_MID_MODELS", "ollama/llama3", "malformed (not a model list)", id="local"
    ),
    pytest.param(
        "PERSONA_MID_MODELS",
        "openrouter/z-ai/glm-flash,nvidia/meta/llama-3.3-70b-instruct",
        "retired model at position 2",
        id="retired",
    ),
    pytest.param("PERSONA_OPENROUTER_SUBSCRIPTION_MODE", None, "missing", id="mode-absent"),
    pytest.param("PERSONA_OPENROUTER_SUBSCRIPTION_MODE", "", "missing", id="mode-empty"),
    pytest.param(
        "PERSONA_OPENROUTER_SUBSCRIPTION_MODE",
        "Paid",
        "invalid (must be exactly free or paid)",
        id="mode-capitalised",
    ),
    pytest.param(
        "PERSONA_OPENROUTER_SUBSCRIPTION_MODE",
        " paid",
        "invalid (must be exactly free or paid)",
        id="mode-with-a-space",
    ),
    pytest.param(
        "PERSONA_OPENROUTER_SUBSCRIPTION_MODE",
        _FAKE_KEY,
        "invalid (must be exactly free or paid)",
        id="mode-holding-a-key",
    ),
]


@pytest.mark.parametrize(("name", "value", "state"), _PROBLEMS)
def test_every_bad_setting_is_named_with_its_state_and_its_value_never_printed(
    vdc: ModuleType, capsys: pytest.CaptureFixture[str], name: str, value: str | None, state: str
) -> None:
    env = {k: v for k, v in _VALID.items() if k != name}
    if value is not None:
        env[name] = value
    code, lines = _run(vdc, capsys, env)
    assert code == 1
    assert lines == [
        *_lines(**{name: state}),
        f"1 of 7 settings are not valid. {_NOTHING_WRITTEN}",
    ]
    printed = "\n".join(lines)
    for fragment in (_FAKE_KEY, "sk-or", "glm-4.6", "llama-3.3-70b", "Paid"):
        assert fragment not in printed


def test_several_bad_settings_are_all_named_in_one_run(
    vdc: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    env = {**_VALID, "PERSONA_FRONTIER_MODELS": "", "PERSONA_OPENROUTER_SUBSCRIPTION_MODE": "x"}
    code, lines = _run(vdc, capsys, env)
    assert code == 1
    assert lines == [
        *_lines(
            PERSONA_FRONTIER_MODELS="missing",
            PERSONA_OPENROUTER_SUBSCRIPTION_MODE="invalid (must be exactly free or paid)",
        ),
        f"2 of 7 settings are not valid. {_NOTHING_WRITTEN}",
    ]


def test_a_value_bearing_failure_names_only_its_type(
    vdc: ModuleType, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def _explode(env: dict[str, str]) -> list[object]:
        raise RuntimeError(f"{env['PERSONA_MID_MODELS']} {_FAKE_KEY}")

    monkeypatch.setattr(vdc, "model_chains", _explode)
    code, lines = _run(vdc, capsys, _VALID)
    assert code == 1
    assert lines == ["validate_deploy_chains failed (RuntimeError). It prints no values."]
