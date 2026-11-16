"""The forget preview offers the members' own evidence (R9-287 round 2 LOW-1).

A forget takes every node merged into the forgotten one (D-R9-287-forget-canonical), so the
preview matches their content too, not only the canonical's.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from persona_api.services import memory_service


class _Episodic:
    queried: list[str]

    def __init__(self, **_kwargs: Any) -> None:  # noqa: ANN401
        pass

    def query(self, persona_id: str, text: str, top_k: int) -> list[Any]:  # noqa: ARG002
        _Episodic.queried.append(text)
        if text == "what the member said":
            return [SimpleNamespace(id="c-member", text="the member's evidence", distance=0.1)]
        return []


class _Store:
    def get_node(self, owner_id: str, node_id: str) -> Any:  # noqa: ANN401, ARG002
        return SimpleNamespace(content="what the canonical says")


def test_a_members_own_evidence_is_offered(monkeypatch: pytest.MonkeyPatch) -> None:
    _Episodic.queried = []
    monkeypatch.setattr(memory_service, "EpisodicStore", _Episodic)
    monkeypatch.setattr(
        memory_service, "_list_all_owner_personas", lambda _e: [{"id": "p1", "yaml": ""}]
    )
    monkeypatch.setattr(memory_service, "_member_texts", lambda *_a: ["what the member said"])

    candidates = memory_service.forget_preview(
        graph_store=_Store(),  # type: ignore[arg-type]
        memory_backend=object(),  # type: ignore[arg-type]
        audit_logger=object(),  # type: ignore[arg-type]
        rls_engine=object(),  # type: ignore[arg-type]
        owner_id="u1",
        node_id="n1",
        floor=0.5,
    )

    assert _Episodic.queried == ["what the canonical says", "what the member said"]
    assert [c.chunk_id for c in candidates] == ["c-member"]
