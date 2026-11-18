"""The worker's synthesizer records the durable disclosure fact (R9-253).

Built-but-inert is the failure class: a ``Synthesizer`` composed without its recorder would
pass every runtime test and record nothing in production. This captures what the worker
root hands ``build_synthesizer`` and stops there (no database is opened).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from persona_api.background import worker_root
from persona_api.config import APIConfig
from persona_api.initiative.disclosures import PgDisclosureStore
from sqlalchemy import create_engine

if TYPE_CHECKING:
    from pathlib import Path


class _Built(Exception):  # noqa: N818 - a control-flow signal, not an error
    def __init__(self, kwargs: dict[str, Any]) -> None:
        super().__init__("captured")
        self.kwargs = kwargs


def test_the_worker_synthesizer_records_on_the_rls_engine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def capture(**kwargs: Any) -> None:  # noqa: ANN401
        raise _Built(kwargs)

    monkeypatch.setattr(worker_root, "build_synthesizer", capture)
    engine = create_engine("sqlite://")
    try:
        with pytest.raises(_Built) as built:
            worker_root.build_worker_registry(
                rls_engine=engine,
                embedder=object(),  # type: ignore[arg-type]
                tier_registry=object(),  # type: ignore[arg-type]
                free_tier_registry=None,
                config=APIConfig(audit_root=str(tmp_path)),
                synthesis_tier="small",
            )
    finally:
        engine.dispose()
    recorder = built.value.kwargs["disclosures"]
    assert isinstance(recorder, PgDisclosureStore)
    assert recorder._engine is engine  # noqa: SLF001 - the owner-scoped engine, not another
