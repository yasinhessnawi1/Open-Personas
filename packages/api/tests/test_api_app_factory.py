"""Unit tests for the app factory + APIConfig (spec 08, T01).

No DB needed — these boot the app with an injected config and assert the
FastAPI instance, config loading, and the unknown-key tolerance.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from loguru import logger as _loguru_logger
from persona.backends import BackendConfig
from persona_api.app import _warn_if_cloud_free_registry_empty, create_app
from persona_api.config import APIConfig, Edition
from persona_runtime.tier import TierConfig, TierRegistry

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def loguru_info_capture() -> Iterator[list[str]]:
    """Loguru sink capturing INFO+ lines (persona.logging wraps loguru, so
    stdlib ``caplog`` sees nothing — the test_api_boot_hermetic.py pattern,
    widened from WARNING to INFO for the N7-T4b startup summary)."""
    captured: list[str] = []
    sink_id = _loguru_logger.add(lambda msg: captured.append(str(msg)), level="INFO")
    try:
        yield captured
    finally:
        _loguru_logger.remove(sink_id)


def test_create_app_returns_fastapi_instance() -> None:
    # Community edition: a no-infra boot (no DSN needed, the cloud-config guard
    # no-ops). The factory smoke tests assert the app exists / serves OpenAPI.
    app = create_app(APIConfig(edition=Edition.community))
    assert isinstance(app, FastAPI)
    assert app.title == "Persona API"


def test_app_boots_with_test_client() -> None:
    # A trivial client boot: the app starts and serves the auto OpenAPI doc.
    # Community edition: a no-infra boot (no DSN needed, the cloud-config guard
    # no-ops). The factory smoke tests assert the app exists / serves OpenAPI.
    app = create_app(APIConfig(edition=Edition.community))
    with TestClient(app) as client:
        resp = client.get("/openapi.json")
        assert resp.status_code == 200
        assert resp.json()["info"]["title"] == "Persona API"


def test_startup_logs_the_mcp_mechanisms_summary(
    loguru_info_capture: list[str],
) -> None:
    """N7-T4b (D-N7-5): ONE structured INFO line names which MCP mechanisms
    THIS DEPLOYMENT can actually exercise, logged once the real lifespan runs.

    Community edition, zero MCP env configured (beyond the suite-wide hermetic
    ``PERSONA_MCP_MIRROR_PATH`` conftest sets for every test — see
    conftest.py's autouse mirror-snapshot fixture): builtin-launcher falls
    back to the catalog's default-enabled subset (never empty), the
    gateway/per-tenant runtime/oauth-provider mechanisms are all off/none,
    byo is always on (no gate), and the mirror names that tmp override path
    (proving the resolve-of-override branch, not just the bundled default).
    No secret (a token, if one existed) ever rides this line.
    """
    app = create_app(APIConfig(edition=Edition.community))
    with TestClient(app):
        pass  # the real _lifespan ran; the summary line already logged.
    summary_lines = [line for line in loguru_info_capture if "MCP mechanisms:" in line]
    assert len(summary_lines) == 1, loguru_info_capture
    line = summary_lines[0]
    assert "builtin-launcher=" in line
    assert "builtin-launcher=none" not in line  # the catalog default subset
    assert "gateway=off" in line
    assert "per-tenant-runtime=off (reason: unconfigured)" in line
    assert "byo=on" in line
    assert "oauth-providers=none" in line
    # The conftest-wide hermetic override, not the bundled snapshot — proves
    # the volume-path branch of the mirror=<bundled|volume:<path>> summary.
    assert "mirror=volume:" in line
    assert "mirror.json" in line


def test_config_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost:5432/db")
    monkeypatch.setenv("PERSONA_API_RATE_LIMIT_MESSAGES", "13")
    cfg = APIConfig()
    assert cfg.database_url == "postgresql+psycopg://u:p@localhost:5432/db"
    assert cfg.rate_limit_messages == 13


def test_config_ignores_unknown_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    # extra="ignore": an unrelated PERSONA_API_* key must not crash construction.
    monkeypatch.setenv("PERSONA_API_SOMETHING_UNRELATED", "x")
    cfg = APIConfig()
    assert cfg.rate_limit_default == 60


def test_authoring_sampling_defaults() -> None:
    # Drafter creativity: temperature defaults hot (0.9); top_p/top_k unset.
    cfg = APIConfig()
    assert cfg.authoring_temperature == 0.9
    assert cfg.authoring_top_p is None
    assert cfg.authoring_top_k is None


def test_authoring_sampling_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSONA_API_AUTHORING_TEMPERATURE", "1.1")
    monkeypatch.setenv("PERSONA_API_AUTHORING_TOP_P", "0.9")
    monkeypatch.setenv("PERSONA_API_AUTHORING_TOP_K", "40")
    cfg = APIConfig()
    assert cfg.authoring_temperature == 1.1
    assert cfg.authoring_top_p == 0.9
    assert cfg.authoring_top_k == 40


def test_effective_app_url_prefers_app_dsn_and_coerces_async() -> None:
    cfg = APIConfig(
        database_url="postgresql+asyncpg://owner@h/db",
        app_database_url="postgresql+asyncpg://persona_app@h/db",
    )
    # prefers the app DSN, coerces +asyncpg -> +psycopg (D-07-1)
    assert cfg.effective_app_database_url == "postgresql+psycopg://persona_app@h/db"


def test_effective_app_url_falls_back_to_database_url() -> None:
    cfg = APIConfig(database_url="postgresql+psycopg://owner@h/db", app_database_url="")
    assert cfg.effective_app_database_url == "postgresql+psycopg://owner@h/db"


def test_jwt_algorithms_list() -> None:
    cfg = APIConfig(jwt_algorithms="HS256, RS256")
    assert cfg.jwt_algorithms_list == ["HS256", "RS256"]


# ---------------------------------------------------------------------------
# R9-059 — startup WARNING when cloud + an empty free-tier registry.
#
# M4 fail-closed semantics (D-M4-4) mean an unconfigured PERSONA_FREE_*_MODELS
# yields an EMPTY (non-None) registry, silently handed to every free-plan /
# no-subscription caller. ``_warn_if_cloud_free_registry_empty`` is the pure,
# directly-testable unit ``_lifespan`` calls right after building that
# registry (mirrors the ``_compose_image_backend`` / ``_resolve_openrouter_
# subscription_mode`` pattern of unit-testing app.py helpers without a full
# DB-backed lifespan boot).
# ---------------------------------------------------------------------------


def _backend_cfg() -> BackendConfig:
    return BackendConfig(provider="anthropic", model="claude-test", api_key="sk-test")


def test_warns_when_cloud_and_free_registry_empty(
    loguru_info_capture: list[str],
) -> None:
    empty_free_registry = TierRegistry({})
    _warn_if_cloud_free_registry_empty(APIConfig(edition=Edition.cloud), empty_free_registry)
    warnings = [line for line in loguru_info_capture if "PERSONA_FREE_*_MODELS" in line]
    assert len(warnings) == 1, loguru_info_capture
    assert "empty tier registry and fail chat" in warnings[0]


def test_no_warning_when_free_registry_has_tiers(
    loguru_info_capture: list[str],
) -> None:
    non_empty_free_registry = TierRegistry(
        {"frontier": TierConfig(name="frontier", backend_config=_backend_cfg())}
    )
    _warn_if_cloud_free_registry_empty(APIConfig(edition=Edition.cloud), non_empty_free_registry)
    assert not [line for line in loguru_info_capture if "PERSONA_FREE_*_MODELS" in line]


def test_no_warning_when_free_registry_is_none(
    loguru_info_capture: list[str],
) -> None:
    # Community — no free-tier gating at all (RuntimeFactory short-circuits on None).
    _warn_if_cloud_free_registry_empty(APIConfig(edition=Edition.cloud), None)
    assert not [line for line in loguru_info_capture if "PERSONA_FREE_*_MODELS" in line]


def test_no_warning_when_community_even_with_empty_registry(
    loguru_info_capture: list[str],
) -> None:
    # Defensive: community never builds a free registry in practice (app.py passes
    # None), but the edition gate must hold even if one were somehow passed.
    empty_free_registry = TierRegistry({})
    _warn_if_cloud_free_registry_empty(APIConfig(edition=Edition.community), empty_free_registry)
    assert not [line for line in loguru_info_capture if "PERSONA_FREE_*_MODELS" in line]
