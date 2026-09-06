"""The connector runtime enables the graph store exactly as the api's lifespan does (R9-125).

A persona that knew the user's budget on the web had "no record" of it on Telegram. The
api calls ``RuntimeFactory.enable_graph_writes`` on its factory (unconditionally; the
factory self-guards on the engine dialect). The connector composition built the same
factory and never made that call, so ``_graph_store`` stayed ``None`` and every
connector turn ran memoryless. This drives the real builder with its heavy
collaborators replaced and asserts the call happens, on the factory it returns, with
the api's audit root.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from persona_api.config import APIConfig
from persona_connectors import service as service_module


class _FakeFactory:
    instances: list[_FakeFactory] = []

    def __init__(self, **kwargs: Any) -> None:  # noqa: ANN401 — mirrors the real kwargs surface
        self.kwargs = kwargs
        self.enabled_with: list[Path] = []
        _FakeFactory.instances.append(self)

    def enable_graph_writes(self, *, audit_root: Path) -> None:
        self.enabled_with.append(audit_root)


@pytest.fixture
def _quiet_composition(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the deploy seams (torch, model backends, network probes) with fakes."""
    _FakeFactory.instances.clear()
    monkeypatch.setattr(service_module, "RuntimeFactory", _FakeFactory)
    monkeypatch.setattr(service_module.persona_service, "default_embedder", lambda _model: object())
    monkeypatch.setattr(service_module, "ChromaBackend", lambda **_kw: MagicMock())
    monkeypatch.setattr(service_module, "PostgresBackend", lambda **_kw: MagicMock())
    monkeypatch.setattr(service_module, "resolve_openrouter_subscription_mode", lambda: None)
    monkeypatch.setattr(service_module, "tier_registry_from_env", lambda **_kw: MagicMock())
    monkeypatch.setattr(service_module, "build_free_tier_registry", lambda _cfg, **_kw: None)
    monkeypatch.setattr(service_module, "PostgresTurnLogWriter", lambda _engine: MagicMock())


@pytest.mark.usefixtures("_quiet_composition")
@pytest.mark.parametrize("edition", ["community", "cloud"])
def test_the_connector_runtime_enables_the_graph_store_like_the_api(
    edition: str, tmp_path: Path
) -> None:
    api_config = APIConfig(edition=edition, audit_root=str(tmp_path / "audit"))

    factory = service_module._build_runtime_factory(  # noqa: SLF001 — the builder under test
        api_config, MagicMock(), credits_policy=MagicMock()
    )

    assert isinstance(factory, _FakeFactory)
    assert factory.enabled_with == [Path(api_config.audit_root)], (
        "the graph store was never enabled; every connector turn would run memoryless"
    )
    assert factory is _FakeFactory.instances[-1], "enable must run on the factory returned"
