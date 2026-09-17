"""The boot line that says what is switched on (R9-170, sweep part2 N).

Production set three feature flags and everything else ran on the code default, and the
process itself could not say which was which: no boot line, no endpoint. The register that
answered the question was built by hand from Fly secrets and code defaults. These pin the
line that answers it from inside the process, and the two properties that make it safe to
print: it reports every boolean the process reads, and it can never print a secret.
"""

from __future__ import annotations

import pytest
from loguru import logger as _loguru_logger
from persona_api.services.flag_report import (
    EXEMPT,
    SCALE_BACKENDS,
    effective_flags,
    log_effective_flags,
)


@pytest.fixture
def unified_recall_on(monkeypatch: pytest.MonkeyPatch) -> str:
    """A real flag the api reads, forced on through its real environment name."""
    import persona.recall.config  # noqa: F401  (the class must be imported, as at boot)

    monkeypatch.setenv("PERSONA_RECALL_UNIFIED_ENABLED", "true")
    return "PERSONA_RECALL_UNIFIED_ENABLED"


def test_an_imported_flag_is_reported_with_its_effective_state(unified_recall_on: str) -> None:
    report = dict(effective_flags())
    assert report.get(unified_recall_on) == "on"


def test_the_same_flag_reads_off_when_the_environment_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import persona.recall.config  # noqa: F401

    monkeypatch.setenv("PERSONA_RECALL_UNIFIED_ENABLED", "false")
    assert dict(effective_flags())["PERSONA_RECALL_UNIFIED_ENABLED"] == "off"


def test_it_never_names_a_secret_or_a_commercial_number() -> None:
    """The register excluded keys, tokens, markup and multipliers by name; so does this.
    A boot line that leaked a key would be worse than the silence it replaces."""
    for name, state in effective_flags():
        assert not EXEMPT.search(name), f"{name} must never be reported"
        assert state in {"on", "off", "unset"}, f"{name} carried a raw value: {state!r}"


def test_the_report_is_sorted_and_has_no_duplicates() -> None:
    names = [name for name, _ in effective_flags()]
    assert names == sorted(names)
    assert len(names) == len(set(names))


def test_the_scale_backends_are_reported_from_the_api_config() -> None:
    """The three R9-170 backends are strings, which is exactly why a register that looked
    for booleans never saw them. They are the settings that must move before the machine
    count does, so they belong on the line."""
    from persona_api.config import APIConfig

    config = APIConfig()
    report = dict(effective_flags(config))
    for field_name in SCALE_BACKENDS:
        env = f"{type(config).model_config.get('env_prefix', '')}{field_name}".upper()
        assert env in report, f"{env} missing from the boot report"
        assert report[env] == str(getattr(config, field_name))


def test_it_is_one_log_line(unified_recall_on: str) -> None:
    captured: list[str] = []
    sink = _loguru_logger.add(lambda m: captured.append(str(m)), level="INFO")
    try:
        log_effective_flags()
    finally:
        _loguru_logger.remove(sink)

    lines = [line for line in captured if "feature flags at boot" in line]
    assert len(lines) == 1, "one greppable line, not one per flag"
    assert f"{unified_recall_on}=on" in lines[0]
