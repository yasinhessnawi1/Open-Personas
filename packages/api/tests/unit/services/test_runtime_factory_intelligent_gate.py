"""The P9-D-4/D-7 global dormancy gate for the Spec-23 IntelligentRouter (T5).

Default OFF: the factory composes NO intelligent router, so
``ConversationLoop._model_selection_active()`` is false for every persona and
the stored per-persona ``intelligent.enabled: true`` (a web-form artifact,
P9-D-7) is never consulted. ``PERSONA_ROUTING_INTELLIGENT_ENABLED`` is the
only activation signal — and it is an operator's deliberate act.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from persona_api.services.runtime_factory import RuntimeFactory
from persona_runtime.routing import IntelligentRouter


class _FakeEmbedder:
    model_name = "fake"
    dimension = 384


class _FakeRegistry:
    def get(self, name: str) -> object:  # pragma: no cover — never reached here
        raise AssertionError(f"no tier lookups expected at construction (asked for {name!r})")


def _factory(api_config: object | None) -> RuntimeFactory:
    return RuntimeFactory(
        rls_engine=object(),  # type: ignore[arg-type]
        embedder=_FakeEmbedder(),  # type: ignore[arg-type]
        tier_registry=_FakeRegistry(),  # type: ignore[arg-type]
        turn_log_writer=object(),  # type: ignore[arg-type]
        audit_root=Path("/tmp/persona-audit-test"),
        api_config=api_config,  # type: ignore[arg-type]
    )


class TestGlobalGateDefaultOff:
    def test_no_api_config_composes_no_intelligent_router(self) -> None:
        assert _factory(None)._intelligent_router is None  # noqa: SLF001

    def test_flag_off_composes_no_intelligent_router(self) -> None:
        cfg = SimpleNamespace(routing_intelligent_enabled=False)
        assert _factory(cfg)._intelligent_router is None  # noqa: SLF001


class TestGlobalGateOn:
    def test_flag_on_composes_the_intelligent_router(self) -> None:
        # The reversible lever (P9-D-4): one env flag re-arms the machinery
        # (after the metadata-repopulation prerequisite).
        cfg = SimpleNamespace(routing_intelligent_enabled=True)
        assert isinstance(_factory(cfg)._intelligent_router, IntelligentRouter)  # noqa: SLF001
