"""A forget that meets a running consolidation forgets nothing (R9-287 review LOW-4).

The node's delete may give up on the graph lock (a 409, try again). It runs before the
episodic evidence is removed, so a forget that gives up leaves the evidence and the node as
they were, and the retry forgets both.
"""

from __future__ import annotations

from typing import Any

import pytest
from persona.graph.errors import GraphNodeBusyError
from persona_api.services import memory_service


class _Episodic:
    removed: list[tuple[str, list[str]]]

    def __init__(self, **_kwargs: Any) -> None:  # noqa: ANN401
        pass

    def remove_documents(self, persona_id: str, chunk_ids: list[str]) -> None:
        _Episodic.removed.append((persona_id, chunk_ids))


@pytest.fixture(autouse=True)
def _episodic(monkeypatch: pytest.MonkeyPatch) -> None:
    _Episodic.removed = []
    monkeypatch.setattr(memory_service, "EpisodicStore", _Episodic)
    monkeypatch.setattr(memory_service, "_list_all_owner_personas", lambda _engine: [{"id": "p1"}])


def _forget() -> None:
    memory_service.forget(
        graph_store=object(),  # type: ignore[arg-type]
        memory_backend=object(),  # type: ignore[arg-type]
        audit_logger=object(),  # type: ignore[arg-type]
        rls_engine=object(),  # type: ignore[arg-type]
        owner_id="u1",
        node_id="n1",
        episodic=[("p1", "c1")],
    )


def test_a_forget_that_gives_up_on_the_graph_removes_no_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _busy(*_a: Any) -> bool:  # noqa: ANN401
        raise GraphNodeBusyError("busy", context={"node_id": "n1"})

    monkeypatch.setattr(memory_service, "delete", _busy)

    with pytest.raises(GraphNodeBusyError):
        _forget()

    assert _Episodic.removed == []


def test_a_forget_removes_the_node_then_the_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []

    def _delete(*_a: Any) -> bool:  # noqa: ANN401
        order.append("node")
        return True

    monkeypatch.setattr(memory_service, "delete", _delete)

    _forget()

    assert order == ["node"]
    assert _Episodic.removed == [("p1", ["c1"])]
