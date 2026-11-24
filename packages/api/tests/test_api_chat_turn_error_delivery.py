"""R9-316: the web chat's registry states that an errored turn's streamed text was delivered.

The web keeps what streamed on screen when a turn ends on an error frame, so a turn whose
answer streamed but whose write-back was refused is charged once. The connectors state the
opposite (``test_connector_origination_wiring.py``). Driven through the REAL lifespan, so
what is asserted is the registry the app actually serves turns from.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from persona_api.app import create_app
from persona_api.config import APIConfig, Edition

if TYPE_CHECKING:
    from pathlib import Path

    from persona.stores.embedder import Embedder


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch: pytest.MonkeyPatch, embedder: Embedder) -> None:
    """No Postgres and no torch (the ``test_api_shutdown_budget.py`` setup)."""
    from persona_api.services import persona_service

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)
    monkeypatch.setattr(persona_service, "default_embedder", lambda *_a, **_k: embedder)


def test_the_web_registry_charges_a_turn_whose_streamed_text_was_shown(tmp_path: Path) -> None:
    config = APIConfig(
        edition=Edition.community,
        community_db_path=tmp_path / "community.db",
        community_memory_path=tmp_path / "chroma",
        workspace_root=tmp_path / "work",
        audit_root=str(tmp_path / "audit"),
    )
    with TestClient(create_app(config)) as client:
        registry = client.app.state.chat_turn_registry
        assert registry is not None
        assert registry.delivers_streamed_text_on_error is True
        assert registry.delivers_images is True
