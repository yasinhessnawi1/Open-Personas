"""``.env.example`` names every per-provider and per-tier knob the code reads.

The families guarded here are the ones a literal grep of the source never finds,
because the code composes them at runtime from a prefix:
``credentials.py`` builds ``PERSONA_{PROVIDER}_BASE_URL`` and ``tier.py`` builds
``PERSONA_{TIER}_{METRIC}``. That is exactly why they went undocumented, and why
prose alone would not keep them documented. An operator who follows
``.env.example`` literally and never learns the mid/small metadata names gets
routing that silently degrades to the heuristic router, with no error.

Read-only: nothing here constructs a settings object or touches the environment.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from typing import get_args

import pytest
from persona.backends.config import DEFAULT_BASE_URLS, Provider
from persona_runtime.tier import (
    _FREE_TIER_ENV_PREFIXES,
    _TIER_ENV_PREFIXES,
    tier_metadata_from_env,
)

_REPO_ROOT = Path(__file__).resolve().parents[4]

# The suffixes ``tier_metadata_from_env`` appends to a tier prefix. Pinned here
# so the documentation gate is explicit, and cross-checked against the reader's
# own source by ``test_the_suffix_list_still_matches_the_reader`` below.
_METADATA_SUFFIXES = (
    "COST_INPUT_PER_1K",
    "COST_OUTPUT_PER_1K",
    "FIRST_TOKEN_LATENCY_MS",
    "THROUGHPUT_TOKENS_PER_SEC",
    "CONTEXT_WINDOW",
    "TOOL_STRENGTH",
    "REASONING_CAPABLE",
    "COST_VERIFIED_AT_DEPLOY",
)


@pytest.fixture(scope="module")
def env_example() -> str:
    """The repo-root ``.env.example``, read once."""
    return (_REPO_ROOT / ".env.example").read_text()


def _is_documented(text: str, name: str) -> bool:
    """Whether ``name`` appears in ``text`` as a variable line, commented or not."""
    return re.search(rf"^#?\s*{re.escape(name)}=", text, re.M) is not None


@pytest.mark.parametrize("provider", sorted(DEFAULT_BASE_URLS))
def test_every_provider_base_url_override_is_documented(env_example: str, provider: str) -> None:
    """Each provider with an HTTP endpoint has a documented base-URL override.

    ``ProviderCredentialResolver.resolve`` reads this for every provider, so an
    operator behind a corporate LLM proxy is always one variable away from
    working. Undocumented, they conclude the product does not support it.
    """
    name = f"PERSONA_{provider.upper()}_BASE_URL"
    assert _is_documented(env_example, name), (
        f"{name} is read by persona.backends.credentials.ProviderCredentialResolver.resolve "
        "but is not in .env.example"
    )


def test_only_the_in_process_provider_lacks_a_base_url() -> None:
    """A new provider cannot dodge the check above by skipping DEFAULT_BASE_URLS.

    ``local`` runs the model in-process through transformers, so it has no
    endpoint and deliberately no documented base URL.
    """
    assert set(get_args(Provider)) - set(DEFAULT_BASE_URLS) == {"local"}


@pytest.mark.parametrize(
    "prefix", sorted({*_TIER_ENV_PREFIXES.values(), *_FREE_TIER_ENV_PREFIXES.values()})
)
@pytest.mark.parametrize("suffix", _METADATA_SUFFIXES)
def test_every_tier_metadata_name_is_documented(env_example: str, prefix: str, suffix: str) -> None:
    """Every tier the runtime reads metadata for has all eight names documented.

    Both the paid tiers and the M4 free tiers go through
    ``tier_metadata_from_env``, so both prefix families need the full list.
    """
    name = f"{prefix}{suffix}"
    assert _is_documented(env_example, name), (
        f"{name} is read by persona_runtime.tier.tier_metadata_from_env but is not in .env.example"
    )


def test_the_suffix_list_still_matches_the_reader() -> None:
    """Pinning the eight names is only safe if drift in the reader is caught."""
    composed = set(
        re.findall(r"\{prefix\}([A-Z][A-Z0-9_]*)", inspect.getsource(tier_metadata_from_env))
    )
    assert composed == set(_METADATA_SUFFIXES)


def test_no_machine_specific_value_leaks_into_the_template(env_example: str) -> None:
    """``.env.example`` is public, so it carries no home directory and no repr.

    One entry once shipped as ``PosixPath('/Users/<name>/.persona')``: a default
    rendered as Python rather than as a value, publishing a username in the first
    file a self-hoster opens.
    """
    offenders = [
        line
        for line in env_example.splitlines()
        if re.search(r"PosixPath\(|WindowsPath\(|/Users/|/home/[a-z]", line)
    ]
    assert offenders == []
