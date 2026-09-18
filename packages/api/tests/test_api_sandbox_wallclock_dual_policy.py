"""Tests for the Spec 25 T08 wall-clock dual policy (D-25-2, D-25-3).

Acceptance criterion 2: env-setup commands get a longer wall-clock cap (120s)
than ordinary code execution (30s), both env-tunable.

Two surfaces are exercised:

1. :func:`persona_api.sandbox.hosted.detect_env_setup` — the small pure
   helper (D-25-2): explicit leading-token match against the package-manager
   set plus the realistic ``subprocess.check_call([sys.executable, "-m",
   "pip", "install", ...])`` shape from the operator log. Pure, so it is
   testable without a live sandbox.

2. :class:`persona_api.sandbox.config.SandboxWallClockConfig` — the
   env-tunable caps (D-25-3): ``PERSONA_SANDBOX_EXEC_CAP_S`` (default 30) and
   ``PERSONA_SANDBOX_SETUP_CAP_S`` (default 120), each with an accepted
   ``..._WALLCLOCK_..._S`` alias that the canonical name outranks.

3. :meth:`HostedSandbox.execute` cap selection — a pip-install-style code
   string selects the 120s setup cap; a ``time.sleep(35)`` code string keeps
   the 30s exec cap and is killed by the wall-clock guard before it returns;
   env overrides are respected and the applied cap is recorded in the
   timeout-error metadata.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from persona.sandbox.errors import ExecutionTimeoutError
from persona.sandbox.result import ExecutionResult
from persona_api.sandbox.config import SandboxWallClockConfig
from persona_api.sandbox.hosted import HostedSandbox, detect_env_setup
from pydantic import ValidationError

# ============================================================ detect_env_setup


@pytest.mark.parametrize(
    "code",
    [
        # subprocess shape from the operator log (D-25-2 realistic shape).
        'subprocess.check_call([sys.executable, "-m", "pip", "install", "rich"])',
        "subprocess.run([sys.executable, '-m', 'pip', 'install', 'numpy'])",
        # bang/shell-style leading-token forms.
        "!pip install pandas",
        "pip3 install scipy",
        "apt-get install -y graphviz",
        "apt install libpango",
        "wget https://example.com/data.csv",
        "curl -O https://example.com/data.csv",
        "uv pip install httpx",
        # two-token forms.
        "npm install left-pad",
        "yarn add lodash",
        # leading whitespace must not defeat detection.
        "   pip install seaborn",
    ],
)
def test_detect_env_setup_true_for_package_manager_invocations(code: str) -> None:
    """D-25-2: package-manager invocations are detected as env-setup."""
    assert detect_env_setup(code) is True


@pytest.mark.parametrize(
    "code",
    [
        # Ordinary compute — the canonical 30s-cap shape.
        "import time\ntime.sleep(35)",
        "print('hello world')",
        # Merely *mentioning* pip in a string/comment must NOT trip detection
        # (the explicit-prefix-list rationale in D-25-2: no false positives).
        "print('run pip install to add packages')",
        "x = 'pip'  # not actually installing anything",
        "# pip install numpy (documentation only)",
        # npm/yarn bare token without the install/add verb is not a setup verb.
        "npm_version = '9.0'",
        "",
    ],
)
def test_detect_env_setup_false_for_ordinary_code(code: str) -> None:
    """D-25-2: ordinary user code (even if it mentions 'pip') is NOT setup."""
    assert detect_env_setup(code) is False


def test_detect_env_setup_handles_multiline_first_command() -> None:
    """A pip install on a later line is still detected (multi-statement cell)."""
    code = "import os\nprint('preparing')\npip install matplotlib"
    assert detect_env_setup(code) is True


# ====================================================== SandboxWallClockConfig


def test_wallclock_config_defaults_match_d_25_3() -> None:
    cfg = SandboxWallClockConfig(_env_file=None)  # type: ignore[call-arg]
    assert cfg.exec_cap_s == 30.0
    assert cfg.setup_cap_s == 120.0


def test_wallclock_config_env_overrides_respected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PERSONA_SANDBOX_WALLCLOCK_EXEC_S", "45")
    monkeypatch.setenv("PERSONA_SANDBOX_WALLCLOCK_SETUP_S", "200")
    cfg = SandboxWallClockConfig()
    assert cfg.exec_cap_s == 45.0
    assert cfg.setup_cap_s == 200.0


def test_wallclock_config_rejects_non_positive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PERSONA_SANDBOX_WALLCLOCK_EXEC_S", "0")
    with pytest.raises(ValidationError, match="> 0"):
        SandboxWallClockConfig()


# ====================== the caps answer to exactly two names (sweep part2, L)


def _clear_cap_names(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "PERSONA_SANDBOX_EXEC_CAP_S",
        "PERSONA_SANDBOX_SETUP_CAP_S",
        "PERSONA_SANDBOX_WALLCLOCK_EXEC_S",
        "PERSONA_SANDBOX_WALLCLOCK_SETUP_S",
        "EXEC_CAP_S",
        "SETUP_CAP_S",
    ):
        monkeypatch.delenv(name, raising=False)


def test_the_documented_cap_names_are_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """``PERSONA_SANDBOX_EXEC_CAP_S`` / ``_SETUP_CAP_S`` are what .env.example lists."""
    _clear_cap_names(monkeypatch)
    monkeypatch.setenv("PERSONA_SANDBOX_EXEC_CAP_S", "99")
    monkeypatch.setenv("PERSONA_SANDBOX_SETUP_CAP_S", "199")
    cfg = SandboxWallClockConfig()
    assert cfg.exec_cap_s == 99.0
    assert cfg.setup_cap_s == 199.0


def test_the_wallclock_alias_is_still_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """The older ``..._WALLCLOCK_EXEC_S`` spelling keeps working.

    A deployment may already have a secret set under it, and silently ignoring a
    secret somebody set is worse than carrying two names for one knob.
    """
    _clear_cap_names(monkeypatch)
    monkeypatch.setenv("PERSONA_SANDBOX_WALLCLOCK_EXEC_S", "45")
    monkeypatch.setenv("PERSONA_SANDBOX_WALLCLOCK_SETUP_S", "200")
    cfg = SandboxWallClockConfig()
    assert cfg.exec_cap_s == 45.0
    assert cfg.setup_cap_s == 200.0


def test_the_canonical_name_wins_over_the_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    """The name an operator can look up is the one that takes effect.

    The alias used to outrank the documented name, which is an hour lost the next
    time a pip install keeps dying at a cap the operator believes they raised.
    """
    _clear_cap_names(monkeypatch)
    monkeypatch.setenv("PERSONA_SANDBOX_EXEC_CAP_S", "99")
    monkeypatch.setenv("PERSONA_SANDBOX_WALLCLOCK_EXEC_S", "77")
    monkeypatch.setenv("PERSONA_SANDBOX_SETUP_CAP_S", "199")
    monkeypatch.setenv("PERSONA_SANDBOX_WALLCLOCK_SETUP_S", "177")
    cfg = SandboxWallClockConfig()
    assert cfg.exec_cap_s == 99.0
    assert cfg.setup_cap_s == 199.0


def test_a_bare_unprefixed_name_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unrelated ``EXEC_CAP_S`` in a shell or base image must not reach the sandbox."""
    _clear_cap_names(monkeypatch)
    monkeypatch.setenv("EXEC_CAP_S", "55")
    monkeypatch.setenv("SETUP_CAP_S", "155")
    cfg = SandboxWallClockConfig()
    assert cfg.exec_cap_s == 30.0
    assert cfg.setup_cap_s == 120.0


def test_construction_by_field_name_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """The hosted sandbox and its tests build this config by keyword."""
    _clear_cap_names(monkeypatch)
    cfg = SandboxWallClockConfig(exec_cap_s=42.0, setup_cap_s=84.0)
    assert cfg.exec_cap_s == 42.0
    assert cfg.setup_cap_s == 84.0


# ============================================ HostedSandbox cap selection


@pytest.mark.asyncio
async def test_execute_selects_setup_cap_for_pip_install_code() -> None:
    """D-25-3: a pip-install-style code string selects the 120s setup cap.

    We patch ``_execute_sync`` to return immediately and capture the
    ``timeout_s`` it was called with so we can assert the SETUP cap (120s)
    was selected — not the 30s exec cap. The caller passes the conventional
    ``timeout_s=30`` exec baseline; env-setup detection upgrades it.
    """
    sandbox = HostedSandbox()
    captured: dict[str, float] = {}

    def _fast_execute_sync(*_a: object, **kwargs: object) -> ExecutionResult:
        captured["timeout_s"] = float(kwargs["timeout_s"])  # type: ignore[arg-type]
        return ExecutionResult(stdout="ok", stderr="", exit_status=0, outcome="ok", duration_ms=1.0)

    code = 'subprocess.check_call([sys.executable, "-m", "pip", "install", "rich"])'
    with patch.object(sandbox, "_execute_sync", _fast_execute_sync):
        result = await sandbox.execute(code, timeout_s=30.0)

    assert result.outcome == "ok"
    assert captured["timeout_s"] == 120.0


@pytest.mark.asyncio
async def test_execute_keeps_exec_cap_and_kills_long_sleep() -> None:
    """D-25-3: a time.sleep(35) code string keeps the 30s exec cap.

    To keep the test fast we shrink both caps via env so the exec cap is well
    below the sleep duration; the wall-clock guard must fire and raise
    ExecutionTimeoutError carrying the applied-cap metadata.
    """
    cfg = SandboxWallClockConfig(exec_cap_s=0.5, setup_cap_s=120.0)
    sandbox = HostedSandbox(wallclock_config=cfg)

    def _slow_execute_sync(*_a: object, **_k: object) -> ExecutionResult:
        import time

        time.sleep(60.0)
        return ExecutionResult(stdout="", stderr="", exit_status=0, outcome="ok", duration_ms=0.0)

    code = "import time\ntime.sleep(35)"
    with patch.object(sandbox, "_execute_sync", _slow_execute_sync):
        start = asyncio.get_event_loop().time()
        with pytest.raises(ExecutionTimeoutError) as excinfo:
            await sandbox.execute(code)
        elapsed = asyncio.get_event_loop().time() - start

    assert elapsed < 5.0, f"timeout did not fire promptly; elapsed={elapsed:.2f}s"
    ctx = excinfo.value.context
    assert ctx["wall_clock_s"] == "0.5"
    assert ctx["cap_applied"] == "exec"


@pytest.mark.asyncio
async def test_execute_setup_cap_env_override_respected() -> None:
    """D-25-3: the setup cap env override is honoured at cap selection."""
    cfg = SandboxWallClockConfig(exec_cap_s=30.0, setup_cap_s=0.5)
    sandbox = HostedSandbox(wallclock_config=cfg)

    def _slow_execute_sync(*_a: object, **_k: object) -> ExecutionResult:
        import time

        time.sleep(60.0)
        return ExecutionResult(stdout="", stderr="", exit_status=0, outcome="ok", duration_ms=0.0)

    code = "pip install pandas"
    with (
        patch.object(sandbox, "_execute_sync", _slow_execute_sync),
        pytest.raises(ExecutionTimeoutError) as excinfo,
    ):
        await sandbox.execute(code)

    ctx = excinfo.value.context
    assert ctx["wall_clock_s"] == "0.5"
    assert ctx["cap_applied"] == "setup"


@pytest.mark.asyncio
async def test_execute_explicit_timeout_s_is_exec_baseline_for_non_setup() -> None:
    """An explicit caller-supplied ``timeout_s`` is the exec baseline for
    non-setup code, so existing callers that pass ``limits.wall_clock_s``
    keep their per-call exec budget."""
    sandbox = HostedSandbox()
    captured: dict[str, float] = {}

    def _fast_execute_sync(*_a: object, **kwargs: object) -> ExecutionResult:
        captured["timeout_s"] = float(kwargs["timeout_s"])  # type: ignore[arg-type]
        return ExecutionResult(stdout="ok", stderr="", exit_status=0, outcome="ok", duration_ms=1.0)

    with patch.object(sandbox, "_execute_sync", _fast_execute_sync):
        await sandbox.execute("print('compute')", timeout_s=7.0)

    assert captured["timeout_s"] == 7.0


@pytest.mark.asyncio
async def test_execute_default_uses_config_exec_cap_for_non_setup() -> None:
    """When no ``timeout_s`` is passed, non-setup code uses the config exec cap."""
    cfg = SandboxWallClockConfig(exec_cap_s=42.0, setup_cap_s=120.0)
    sandbox = HostedSandbox(wallclock_config=cfg)
    captured: dict[str, float] = {}

    def _fast_execute_sync(*_a: object, **kwargs: object) -> ExecutionResult:
        captured["timeout_s"] = float(kwargs["timeout_s"])  # type: ignore[arg-type]
        return ExecutionResult(stdout="ok", stderr="", exit_status=0, outcome="ok", duration_ms=1.0)

    with patch.object(sandbox, "_execute_sync", _fast_execute_sync):
        await sandbox.execute("print('compute')")

    assert captured["timeout_s"] == 42.0
