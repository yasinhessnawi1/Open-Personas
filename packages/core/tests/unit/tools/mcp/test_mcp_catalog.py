"""Unit tests for the built-in MCP server catalog (Spec 27 T2, D-27-1)."""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING

import pytest
from persona.tools.mcp.catalog import (
    BUILTIN_MCP_CATALOG,
    CATALOG_PATH,
    MCPSecretField,
    MCPServerCatalogEntry,
    authored_server_names,
    default_enabled_server_names,
    known_mcp_server_names,
    load_mcp_catalog,
    mcp_server_entry,
    recommender_provider_tag,
)
from pydantic import ValidationError

if TYPE_CHECKING:
    from pathlib import Path


def test_bundled_catalog_loads_the_six_known_servers() -> None:
    assert known_mcp_server_names() == {
        "time",
        "calculator",
        "filesystem",
        "weather",
        "fetch",
        "github",
    }


def test_default_enabled_is_the_low_risk_authored_subset() -> None:
    # D-27-4: safe subset = filesystem + time + calculator (catalog order).
    assert default_enabled_server_names() == ("time", "calculator", "filesystem")


def test_authored_servers_exclude_byo_external_ones() -> None:
    # D-27-1 / D-27-11: fetch + github are external (Persona ships no code).
    assert authored_server_names() == {"time", "calculator", "filesystem", "weather"}
    assert mcp_server_entry("fetch") is not None
    assert mcp_server_entry("fetch").kind == "external"  # type: ignore[union-attr]
    assert mcp_server_entry("github").kind == "external"  # type: ignore[union-attr]


def test_github_rebound_onto_per_user_oauth() -> None:
    # Spec R8 (R8-D-7): github is rebound OFF the operator-global GITHUB_TOKEN env var
    # onto per-user OAuth — no env-token bypass, an oauth provider binding instead.
    github = mcp_server_entry("github")
    assert github is not None
    assert github.required_env == ()  # no GITHUB_TOKEN bypass
    assert "GITHUB_TOKEN" not in github.required_env
    assert github.auth_method == "oauth"
    assert github.oauth_provider == "github"


def test_no_catalog_entry_keeps_a_github_token_bypass() -> None:
    # The rebind removes the ONLY env-var-token path in the bundled catalog.
    for entry in BUILTIN_MCP_CATALOG.servers.values():
        assert "GITHUB_TOKEN" not in entry.required_env


def test_weather_is_opt_in_not_default_enabled() -> None:
    weather = mcp_server_entry("weather")
    assert weather is not None
    assert weather.kind == "builtin"
    assert weather.default_enabled is False


def test_every_server_carries_gap_detection_keywords() -> None:
    # Spec 27 T11 (D-27-7) needs a non-empty keyword vocabulary per server.
    for entry in BUILTIN_MCP_CATALOG.servers.values():
        assert entry.keywords, f"{entry.name} has no gap-detection keywords"


def test_every_server_has_a_verb_led_capability() -> None:
    # Spec 27 T11: the consent line is "…which would let me {capability}." so the
    # capability MUST be a lower-cased verb phrase (not a capitalised noun phrase),
    # otherwise the question reads ungrammatically ("let me Current weather …").
    for entry in BUILTIN_MCP_CATALOG.servers.values():
        assert entry.capability, f"{entry.name} has no capability phrase"
        assert entry.capability[0].islower(), (
            f"{entry.name} capability must be a lower-cased verb phrase: {entry.capability!r}"
        )
        sentence = f"This would let me {entry.capability}."
        assert sentence.endswith(".")


def test_provider_tag_distinguishes_default_from_opt_in() -> None:
    # spec §2.3: default-enabled built-ins are mcp:builtin; everything that needs
    # an operator step (opt-in built-in OR external) is mcp:optional.
    assert recommender_provider_tag(mcp_server_entry("filesystem")) == "mcp:builtin"  # type: ignore[arg-type]
    assert recommender_provider_tag(mcp_server_entry("weather")) == "mcp:optional"  # type: ignore[arg-type]
    assert recommender_provider_tag(mcp_server_entry("github")) == "mcp:optional"  # type: ignore[arg-type]


def test_unknown_server_returns_none() -> None:
    assert mcp_server_entry("does-not-exist") is None


def test_entry_is_frozen_and_forbids_extra_fields() -> None:
    entry = mcp_server_entry("time")
    assert entry is not None
    with pytest.raises(ValidationError):
        MCPServerCatalogEntry.model_validate(
            {"name": "x", "description": "d", "kind": "builtin", "risk": "low", "bogus": 1}
        )


# -- N1 (D-N1-3): additive display-metadata fields for the Docker catalog mirror ---


def test_actual_builtin_catalog_loads_through_the_extended_model() -> None:
    """Import-regression (D-N1-3): the REAL bundled ``catalog.toml`` must validate.

    Loads the actual on-disk builtin catalog (not a synthetic fixture) through the
    extended ``MCPServerCatalogEntry``. Because the model is ``extra="forbid"`` +
    frozen, any NEW display field added without a default would make every builtin
    row fail to validate at import — this test catches that for real. Every N1
    display field MUST be additive-with-default; the builtin rows declare none of
    them, so they all resolve to their defaults below.
    """
    catalog = load_mcp_catalog(CATALOG_PATH)
    assert catalog.servers, "the bundled catalog must load"
    for entry in catalog.servers.values():
        # New mirror fields default cleanly for builtin rows (which set none of them).
        assert entry.display_name == ""
        assert entry.icon_url == ""
        assert entry.image == ""
        assert entry.source_project == ""
        assert entry.source_commit == ""
        assert entry.signed is False
        assert entry.allow_hosts == ()
        assert entry.secrets == ()
        assert entry.server_type == "builtin"


def test_entry_accepts_mirror_display_metadata() -> None:
    """A mirror-shaped entry (Docker ``server.yaml``) round-trips through the model."""
    entry = MCPServerCatalogEntry.model_validate(
        {
            "name": "github-official",
            "description": "Official GitHub MCP Server.",
            "kind": "external",
            "risk": "medium",
            "display_name": "GitHub Official",
            "icon_url": "https://avatars.githubusercontent.com/u/9919?s=200&v=4",
            "image": "ghcr.io/github/github-mcp-server",
            "server_type": "server",
            "source_project": "https://github.com/github/github-mcp-server",
            "source_commit": "23fa0dd1a821d1346c1de2abafe7327d26981606",
            "signed": True,
            "allow_hosts": ("api.github.com:443", "github.com:443"),
            "secrets": (
                {
                    "name": "github.personal_access_token",
                    "env": "GITHUB_PERSONAL_ACCESS_TOKEN",
                    "example": "<YOUR_TOKEN>",
                    "description": "Create a token on GitHub.",
                },
            ),
        }
    )
    assert entry.display_name == "GitHub Official"
    assert entry.server_type == "server"
    assert entry.source_commit == "23fa0dd1a821d1346c1de2abafe7327d26981606"
    assert entry.signed is True
    assert entry.allow_hosts == ("api.github.com:443", "github.com:443")
    assert len(entry.secrets) == 1
    assert entry.secrets[0].env == "GITHUB_PERSONAL_ACCESS_TOKEN"


def test_secret_field_is_frozen_and_forbids_extra() -> None:
    field = MCPSecretField(name="x.token", env="X_TOKEN")
    assert field.example == ""  # optional display fields default empty
    assert field.description == ""
    with pytest.raises(ValidationError):
        field.env = "Y"  # type: ignore[misc] — frozen → assignment rejected
    with pytest.raises(ValidationError):
        MCPSecretField.model_validate({"name": "x", "env": "X", "bogus": 1})


def test_secrets_are_display_only_no_value_field() -> None:
    """D-N1-5: the mirror carries the secret SCHEMA, never a value.

    ``MCPSecretField`` exposes only name/env/example/description — there is no
    field that could hold a credential value. Credential isolation starts in the
    type: the catalog cannot transport a secret even by accident.
    """
    fields = set(MCPSecretField.model_fields)
    assert fields == {"name", "env", "example", "description"}
    assert "value" not in fields
    assert "credential" not in fields


def test_malformed_catalog_fails_loud(tmp_path: Path) -> None:
    bad = tmp_path / "catalog.toml"
    bad.write_text(
        textwrap.dedent(
            """
            [server.broken]
            kind = "builtin"
            risk = "nonsense-risk-level"
            description = "d"
            """
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        load_mcp_catalog(bad)
