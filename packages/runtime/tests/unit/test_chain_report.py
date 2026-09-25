"""The boot line that names the model chains this process was handed (R9-213).

On 2026-09-20 the owner updated the api's chains, chat moved, calls did not, and nothing
said the update was partial: voice composes its own registry (D-V5-6) and reads its own
copy of every list. These pin the line that makes a mismatch visible from two log reads:
all six settings, unset and empty named as such, and a fingerprint that is equal exactly
when the lists are equal.

They also pin what it must never print. A chain value is operator-typed, and a bad copy
has already reached a production app once: a quoting slip, a pasted ``.env`` line, a URL
with credentials or a key where a model belongs all parse as a model list. Two states keep
them off the line, both whole-chain and without a fingerprint:

* ``withheld``: the value parses, so the registry builder serves it, but an entry is not
  plainly a model slug. The chain is configured, so it counts in "N of 6 set".
* ``malformed``: the value does not parse at all, so no builder serves it as written.

Fake credentials are assembled at runtime so no literal here has a real token's shape.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import pytest
from loguru import logger as _loguru_logger
from persona_runtime import chain_report
from persona_runtime.chain_report import (
    chain_fingerprint,
    format_model_chains,
    log_model_chains_at_boot,
    model_chains,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

#: Written out rather than read from CHAIN_ENV_NAMES, so these tests notice when it changes.
_SIX = (
    "PERSONA_FRONTIER_MODELS",
    "PERSONA_MID_MODELS",
    "PERSONA_SMALL_MODELS",
    "PERSONA_FREE_FRONTIER_MODELS",
    "PERSONA_FREE_MID_MODELS",
    "PERSONA_FREE_SMALL_MODELS",
)

_FAKE_OPENROUTER_KEY = "sk-" + "or-v1-" + "0a1b2c3d" * 8
_FAKE_OPENAI_KEY = "sk-" + "proj-" + "Zz9Yy8Xx" * 6


def _fp(canonical: str) -> str:
    """The documented contract: first 8 hex of sha256 over ``provider/model`` joined by ','."""
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:8]


def _line(configured: int, **segments: str) -> str:
    """The whole expected boot line: every setting ``unset`` unless given."""
    parts = " | ".join(f"{name}={segments.get(name, 'unset')}" for name in _SIX)
    return f"model chains at boot: {configured} of 6 set | {parts}"


@pytest.fixture
def boot_log() -> Iterator[list[tuple[str, str]]]:
    """``(level, message)`` of every record the boot report emits, at any level."""
    captured: list[tuple[str, str]] = []

    def _sink(message: object) -> None:
        record = message.record  # type: ignore[attr-defined]
        if record["message"].startswith("model chains at boot"):
            captured.append((record["level"].name, record["message"]))

    sink = _loguru_logger.add(_sink, level="DEBUG")
    try:
        yield captured
    finally:
        _loguru_logger.remove(sink)


# ---- what the line says --------------------------------------------------------------


def test_every_chain_setting_is_reported_in_order_even_when_nothing_is_set() -> None:
    reports = model_chains(env={})
    assert tuple(r.name for r in reports) == _SIX
    assert [r.state for r in reports] == ["unset"] * 6


def test_unset_and_empty_are_named_explicitly_and_differently() -> None:
    line = format_model_chains(model_chains(env={"PERSONA_MID_MODELS": "   "}))
    assert line == _line(0, PERSONA_MID_MODELS="empty").split(" set | ", 1)[1]


def test_a_set_chain_reports_its_parsed_models_in_chain_order_with_its_fingerprint() -> None:
    env = {"PERSONA_MID_MODELS": "openrouter/z-ai/glm-x,groq/llama-y"}
    mid = model_chains(env=env)[1]
    assert mid.name == "PERSONA_MID_MODELS"
    assert mid.state == "set"
    assert mid.models == ("openrouter/z-ai/glm-x", "groq/llama-y")
    assert mid.fingerprint == _fp("openrouter/z-ai/glm-x,groq/llama-y")


def test_it_is_one_info_line_read_from_the_process_environment(
    monkeypatch: pytest.MonkeyPatch, boot_log: list[tuple[str, str]]
) -> None:
    for name in _SIX:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PERSONA_OPENROUTER_API_KEY", _FAKE_OPENROUTER_KEY)
    monkeypatch.setenv("PERSONA_FRONTIER_MODELS", "openrouter/anthropic/claude-x")
    monkeypatch.setenv("PERSONA_FREE_SMALL_MODELS", "")

    log_model_chains_at_boot()

    assert boot_log == [
        (
            "INFO",
            _line(
                1,
                PERSONA_FRONTIER_MODELS=(
                    f"[openrouter/anthropic/claude-x] fp={_fp('openrouter/anthropic/claude-x')}"
                ),
                PERSONA_FREE_SMALL_MODELS="empty",
            ),
        )
    ]


def test_long_real_slugs_and_workers_ai_slugs_are_printed(
    boot_log: list[tuple[str, str]],
) -> None:
    """The display rule must not call real catalogue entries malformed: long slugs, ``:free``
    suffixes, and Cloudflare Workers AI ids, which begin ``@cf/``."""
    chain = (
        "openrouter/nvidia/llama-3.3-nemotron-super-49b-v1.5,"
        "openrouter/nvidia/nemotron-3-super-120b-a12b:free,"
        "cloudflare/@cf/meta/llama-4-scout-17b-16e-instruct"
    )
    log_model_chains_at_boot(env={"PERSONA_SMALL_MODELS": chain})
    assert boot_log == [
        ("INFO", _line(1, PERSONA_SMALL_MODELS=f"[{chain}] fp={_fp(chain)}")),
    ]


# ---- the fingerprint -----------------------------------------------------------------


def test_surrounding_and_inner_whitespace_fingerprint_like_the_clean_form() -> None:
    clean = "openrouter/z-ai/glm-x,groq/llama-y"
    messy = "  openrouter / z-ai/glm-x ,\t groq/ llama-y  "
    tidy = model_chains(env={"PERSONA_MID_MODELS": clean})[1]
    loose = model_chains(env={"PERSONA_MID_MODELS": messy})[1]
    assert loose.state == "set"
    assert loose.models == tidy.models == ("openrouter/z-ai/glm-x", "groq/llama-y")
    assert loose.fingerprint == tidy.fingerprint == _fp(clean)


def test_different_content_gives_a_different_fingerprint() -> None:
    assert chain_fingerprint(("openrouter/a/b", "groq/c")) != chain_fingerprint(
        ("openrouter/a/b", "groq/d")
    )


def test_order_is_part_of_the_chain_so_it_is_part_of_the_fingerprint() -> None:
    """Fallback order is meaningful (D-20-4): the same models in another order is another chain."""
    assert chain_fingerprint(("openrouter/a/b", "groq/c")) != chain_fingerprint(
        ("groq/c", "openrouter/a/b")
    )


def test_the_fingerprint_is_the_documented_sha256_prefix() -> None:
    assert chain_fingerprint(("openrouter/a/b", "groq/c")) == _fp("openrouter/a/b,groq/c")


# ---- what it must never print --------------------------------------------------------

_NEVER_PRINTED = [
    pytest.param(
        f"openrouter/z-ai/glm-4.6 PERSONA_OPENROUTER_API_KEY={_FAKE_OPENROUTER_KEY}",
        id="shell-quoting-slip",
    ),
    pytest.param(f"openai/{_FAKE_OPENAI_KEY}", id="key-as-model-name"),
    pytest.param(
        f"openrouter/z-ai/glm-4.6\nPERSONA_OPENROUTER_API_KEY={_FAKE_OPENROUTER_KEY}",
        id="env-paste-with-newline",
    ),
    pytest.param("openrouter/https://user:pa55@host.example/x", id="url-with-credentials"),
    pytest.param("openrouter/https://host.example/x", id="url-without-credentials"),
    pytest.param("openrouter/" + "0123456789abcdef" * 3, id="opaque-hex-tail"),
    pytest.param("openrouter/z-ai/glm-4.6\tfree", id="tab"),
    pytest.param("openrouter/z-ai/glm\x07bell", id="control-character"),
    # Each of these is caught by exactly one rule, so each rule is proven on its own.
    pytest.param("openrouter/" + "AKIA" + "IOSFODNN7EXAMPLE", id="aws-key-id-as-model"),
    pytest.param("openai/" + "sk-" + "proj-ab12cd34-ef56gh78-ij90", id="short-key-as-model"),
    pytest.param("openrouter/user:pa55@host.example", id="credentials-without-a-scheme"),
    pytest.param("openrouter/z-ai/glm-4.6=hunter2", id="equals-without-a-known-label"),
    pytest.param("openrouter/z-ai/glm-4.6 hunter2", id="space-then-a-password"),
    pytest.param(
        f"openrouter/z-ai/glm-4.6,openai/{_FAKE_OPENAI_KEY}",
        id="one-bad-entry-poisons-the-whole-chain",
    ),
]


@pytest.mark.parametrize("value", _NEVER_PRINTED)
def test_a_served_chain_that_is_not_plainly_model_slugs_is_withheld_and_still_counted(
    value: str, boot_log: list[tuple[str, str]]
) -> None:
    """Each of these parses, so the builder serves it: it is configured, not broken. Its
    content is what nobody vetted, so the line says ``withheld`` and counts it as set."""
    report = model_chains(env={"PERSONA_MID_MODELS": value})[1]
    assert (report.state, report.models, report.fingerprint) == ("withheld", (), None)

    log_model_chains_at_boot(env={"PERSONA_MID_MODELS": value})

    assert boot_log == [("INFO", _line(1, PERSONA_MID_MODELS="withheld"))]


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(_FAKE_OPENROUTER_KEY, id="bare-key-no-slash"),
        pytest.param("openrouter/z-ai/glm-x,,groq/llama-y", id="empty-entry"),
        pytest.param("mystery/z-ai/glm-x", id="unknown-provider"),
    ],
)
def test_a_value_that_does_not_parse_is_malformed_and_not_counted(
    value: str, boot_log: list[tuple[str, str]]
) -> None:
    report = model_chains(env={"PERSONA_MID_MODELS": value})[1]
    assert (report.state, report.models, report.fingerprint) == ("malformed", (), None)

    log_model_chains_at_boot(env={"PERSONA_MID_MODELS": value})

    assert boot_log == [("INFO", _line(0, PERSONA_MID_MODELS="malformed"))]


@pytest.mark.parametrize("value", ["ollama/llama3", "local/qwen2.5-7b"])
def test_a_local_provider_entry_is_malformed_and_boot_carries_on(
    value: str, boot_log: list[tuple[str, str]]
) -> None:
    """The parser rejects local providers in a MODELS list with its own exception type. The
    free builder tolerates that value; the boot line must as well, not take the process down."""
    log_model_chains_at_boot(env={"PERSONA_FREE_MID_MODELS": value})
    assert boot_log == [("INFO", _line(0, PERSONA_FREE_MID_MODELS="malformed"))]


def test_an_unbroken_run_of_31_prints_and_a_run_of_32_is_withheld(
    boot_log: list[tuple[str, str]],
) -> None:
    """The opaque-run rule's boundary, pinned in both directions: 31 letters and digits in a
    row is still a slug, 32 is how a hex or base62 key looks."""
    run = "a1b2c3d4e5f6" * 3
    printed = f"openrouter/vendor/{run[:31]}"
    withheld = f"openrouter/vendor/{run[:32]}"

    log_model_chains_at_boot(
        env={"PERSONA_FRONTIER_MODELS": printed, "PERSONA_MID_MODELS": withheld}
    )

    assert boot_log == [
        (
            "INFO",
            _line(
                2,
                PERSONA_FRONTIER_MODELS=f"[{printed}] fp={_fp(printed)}",
                PERSONA_MID_MODELS="withheld",
            ),
        )
    ]


def test_the_documented_residual_a_short_bare_secret_after_a_provider_still_prints(
    boot_log: list[tuple[str, str]],
) -> None:
    """Owner ruling (R9-213, option a): the line prints slug-shaped names, not only catalogue
    ids. So a short password with no recognisable shape, typed straight after a valid
    ``provider/``, looks like a model and is printed. This pins that residual so the module
    docstring's statement of it stays true; changing it is a ruling, not a refactor."""
    value = "openrouter/hunter2-summer24"
    log_model_chains_at_boot(env={"PERSONA_MID_MODELS": value})
    assert boot_log == [("INFO", _line(1, PERSONA_MID_MODELS=f"[{value}] fp={_fp(value)}"))]


def test_a_report_that_fails_logs_one_warning_and_never_raises(
    monkeypatch: pytest.MonkeyPatch, boot_log: list[tuple[str, str]]
) -> None:
    """A boot report must never stop api or voice from starting, and its failure must not
    carry a value either: only the exception's type is named."""

    def _explode(tier_name: str, raw_value: str) -> list[tuple[str, str]]:
        raise RuntimeError(f"{tier_name} {raw_value} {_FAKE_OPENROUTER_KEY}")

    monkeypatch.setattr(chain_report, "parse_models_list", _explode)

    log_model_chains_at_boot(env={"PERSONA_MID_MODELS": "openrouter/z-ai/glm-x"})

    assert boot_log == [("WARNING", "model chains at boot: report unavailable (RuntimeError)")]
