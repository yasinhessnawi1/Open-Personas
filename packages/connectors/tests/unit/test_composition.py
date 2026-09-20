"""ConnectorComposition — the composition root (Spec C1 T1, C1-D-1).

The single api-coupled module: it assembles the connector service's shared
foundations by reusing persona-api building blocks (the edition switch, the RLS
engine, the ``current_user_id`` owner-scope — the run_worker.py pattern,
D-C1-X-rls-spine). The delivery-router + conversation-loop wiring are seamed for
T9/T10; T1 proves the foundations construct and the owner-scope set/reset is the
real persona-api contextvar (so RLS scopes every store touch).
"""

from __future__ import annotations

import pytest
from persona_api.config import Edition
from persona_api.middleware.rls_context import current_user_id
from persona_api.services.delivery_router import DeliveryRouter
from persona_api.services.origination_delivery import ChannelDeliverers, build_origination_router
from persona_connectors.composition import ConnectorComposition
from persona_connectors.config import ConnectorConfig
from persona_connectors.errors import ConnectorError
from sqlalchemy.engine import Engine

_CLOUD_URL = "postgresql+psycopg://u:p@localhost:5432/db"


def test_exposes_edition_enum_for_community() -> None:
    """The string config edition is surfaced as the persona-api Edition enum."""
    comp = ConnectorComposition(ConnectorConfig(edition="community"))
    assert comp.edition is Edition.community


def test_exposes_edition_enum_for_cloud() -> None:
    comp = ConnectorComposition(ConnectorConfig(edition="cloud"))
    assert comp.edition is Edition.cloud


def test_make_engine_cloud_returns_engine_without_connecting() -> None:
    """Cloud: make_engine builds an RLS-scoped Engine (lazy — no DB connection)."""
    config = ConnectorConfig(
        edition="cloud", database_url="postgresql+psycopg://u:p@localhost:5432/db"
    )
    comp = ConnectorComposition(config)
    engine = comp.make_engine()
    assert isinstance(engine, Engine)


def test_make_engine_cloud_without_url_fails_fast() -> None:
    """Cloud edition with no database_url is a misconfiguration — fail fast (ENG-STD)."""
    comp = ConnectorComposition(ConnectorConfig(edition="cloud", database_url=""))
    with pytest.raises(ConnectorError):
        comp.make_engine()


def test_owner_scope_sets_the_persona_api_contextvar() -> None:
    """The run_worker.py RLS spine (D-C1-X-rls-spine): owner_scope sets the EXACT
    persona-api ``current_user_id`` contextvar the RLS engine listener reads, so
    every store touch inside the scope is owner-scoped.
    """
    comp = ConnectorComposition(ConnectorConfig())
    assert current_user_id.get() is None
    with comp.owner_scope("user_abc"):
        assert current_user_id.get() == "user_abc"
    assert current_user_id.get() is None


def test_owner_scope_resets_even_on_exception() -> None:
    """The contextvar is reset in a finally — an error mid-scope never leaks the owner."""
    comp = ConnectorComposition(ConnectorConfig())

    def _raise_inside_scope() -> None:
        with comp.owner_scope("user_xyz"):
            assert current_user_id.get() == "user_xyz"
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        _raise_inside_scope()
    assert current_user_id.get() is None


class _FakeDeliverer:
    """A minimal C0 ``MessageDeliverer`` stand-in (registers into the router)."""

    async def deliver(self, message: object) -> object:  # noqa: ARG002 — protocol shape
        return None


def test_multiple_connectors_are_reachable_side_by_side() -> None:
    """C3 multi-connector wiring, now read through the seam that actually routes.

    This used to build ``composition.build_delivery_router`` and assert the result was a
    router. It was true and it was worthless: the service entry discarded that router, so
    the deliverers were reachable in this test and nowhere else (R9-120). The property C3
    cares about is unchanged, so it is asserted where it now lives, against the registry
    the one origination router reads.
    """
    engine = ConnectorComposition(
        ConnectorConfig(edition="cloud", database_url=_CLOUD_URL)
    ).make_engine()
    channels = ChannelDeliverers()
    channels.bind(
        {
            "telegram": _FakeDeliverer(),  # type: ignore[dict-item]
            "discord": _FakeDeliverer(),  # type: ignore[dict-item]
            "slack": _FakeDeliverer(),  # type: ignore[dict-item]
        }
    )

    router = build_origination_router(rls_engine=engine, channels=channels)

    assert isinstance(router, DeliveryRouter)
    # The web home rides alongside, so a conversation that is not on any connector still
    # has a target and the router's no-silent-drop guard holds.
    assert sorted(router._deliverers) == ["discord", "slack", "telegram", "web"]  # noqa: SLF001
