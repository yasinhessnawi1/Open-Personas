"""Forgetting a conversation's memory takes the facts learned from it out of the graph (R9-304).

The rule, on provenance trails (the persona erasure's, with the conversation as the source): a
fact only this conversation supports goes; a fact another conversation (or the user, or another
source) also supports keeps everything but this conversation's entries; the SELF node never
goes. Consolidation's own bookkeeping is nobody's support. The step runs in the transaction that
deletes the conversation's row: the graph lock first, then the conversation's row, then the
graph. The SQL runs on Postgres in CI
(tests/integration/test_forget_conversation_graph_postgres.py).
"""

from __future__ import annotations

from typing import Any

import pytest
from persona_api.services import forget_conversation_graph as fcg
from persona_api.services.erasure_graph import plan_trails_from

_C = "conv-forgotten"
_D = "conv-kept"
_SELF = "user_alice::self"
_AT = "2026-11-18T12:00:00Z"


def _from(conversation: str | None, *, source: str = "persona_self") -> dict[str, object]:
    return {
        "source": source,
        "persona_id": "persona_a",
        "interaction_id": conversation,
        "interaction_kind": "conversation" if conversation else None,
        "written_at": _AT,
        "grounding": f"what was said in {conversation}",
    }


_CONSOLIDATION = {
    "source": "system",
    "written_at": _AT,
    "reason": 'consolidation:run1 aliases:["x"]',
}


def _plan(*rows: tuple[str, list[dict[str, object]]]):  # noqa: ANN202
    return plan_trails_from(
        ((key, key, trail) for key, trail in rows),
        source=fcg.conversation_source(_C),
        self_node_id=_SELF,
    )


def test_a_fact_only_this_conversation_supports_is_removed() -> None:
    plan = _plan(("n1", [_from(_C), _from(_C)]), ("n2", [_from(_C), _CONSOLIDATION]))

    assert plan.doomed == ["n1", "n2"], "consolidation's bookkeeping is no one's support"
    assert plan.rewrites == {}


def test_a_fact_another_conversation_supports_loses_only_this_ones_entries() -> None:
    kept = _from(_D)
    plan = _plan(("n1", [_from(_C), kept, _from(_C)]))

    assert plan.doomed == []
    assert plan.rewrites == {"n1": [kept]}


def test_the_same_persona_in_another_conversation_is_other_support() -> None:
    """The source is the conversation, not the persona that had it."""
    elsewhere = {**_from(None), "reason": "direct write"}
    plan = _plan(("n1", [_from(_C), elsewhere]))

    assert plan.rewrites == {"n1": [elsewhere]}


def test_a_users_own_edit_keeps_the_fact() -> None:
    edit = {"source": "user", "written_at": _AT, "reason": "user correction"}
    plan = _plan(("n1", [_from(_C), edit]))

    assert plan.rewrites == {"n1": [edit]}


def test_the_self_node_keeps_dated_stubs_of_this_conversation() -> None:
    plan = _plan((_SELF, [_from(_C)]))

    assert plan.doomed == []
    assert plan.rewrites == {_SELF: [{"source": "persona_self", "written_at": _AT}]}


def test_a_fact_without_this_conversation_is_left_alone() -> None:
    plan = _plan(("n1", [_from(_D)]))

    assert plan.doomed == []
    assert plan.rewrites == {}


def test_the_preview_counts_the_facts_the_user_sees() -> None:
    """Merged members are duplicates hidden behind their canonical, and SELF is the user."""
    rows = [
        ("gone", [_from(_C)], None),
        ("gone_member", [_from(_C)], "kept"),
        ("kept", [_from(_C), _from(_D)], None),
        ("kept_member", [_from(_C), _from(_D)], "kept"),
        ("untouched", [_from(_D)], None),
        (_SELF, [_from(_C)], None),
    ]

    preview = fcg.preview_counts(rows, conversation_id=_C, self_node_id=_SELF)

    assert preview == fcg.ConversationGraphPreview(facts_removed=1, facts_kept=1)


class _Result:
    def __init__(self, row: object) -> None:
        self._row = row

    def first(self) -> object:
        return self._row


class _Dialect:
    def __init__(self, name: str) -> None:
        self.name = name


class _Conn:
    """Logs each statement; the conversation's row is there unless ``missing``."""

    def __init__(self, *, dialect: str = "postgresql", missing: bool = False) -> None:
        self.dialect = _Dialect(dialect)
        self.missing = missing
        self.sql: list[str] = []

    def execute(self, stmt: Any, params: dict[str, str] | None = None) -> _Result:  # noqa: ANN401, ARG002
        self.sql.append(str(stmt))
        return _Result(None if self.missing and "conversations" in str(stmt) else ("c",))


@pytest.fixture
def taken(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, str]]]:
    calls: list[tuple[str, dict[str, str]]] = []

    def _take(_conn: object, owner_id: str, source: dict[str, str]) -> dict[str, int]:
        calls.append((owner_id, dict(source)))
        return {"nodes_deleted": 2}

    monkeypatch.setattr(fcg, "take_source_out", _take)
    return calls


def test_the_lock_comes_first_then_the_conversations_row_then_the_graph(
    taken: list[tuple[str, dict[str, str]]],
) -> None:
    conn = _Conn()

    counts = fcg.forget_conversation_graph(conn, owner_id="u1", conversation_id=_C)  # type: ignore[arg-type]

    assert counts == {"nodes_deleted": 2}
    assert conn.sql[0].startswith("SET LOCAL lock_timeout = '15s'"), "bounded, like a delete"
    assert "pg_advisory_xact_lock" in conn.sql[1], "the graph lock before any row lock"
    assert "FROM conversations" in conn.sql[2]
    assert conn.sql[2].rstrip().endswith("FOR UPDATE"), "waits for writes in flight, stops new"
    assert taken == [("u1", {"interaction_id": _C})]


def test_a_conversation_that_is_not_the_callers_touches_no_graph(
    taken: list[tuple[str, dict[str, str]]],
) -> None:
    conn = _Conn(missing=True)

    assert fcg.forget_conversation_graph(conn, owner_id="u1", conversation_id=_C) == {}  # type: ignore[arg-type]
    assert taken == []


def test_off_postgres_there_is_no_graph(taken: list[tuple[str, dict[str, str]]]) -> None:
    conn = _Conn(dialect="sqlite")

    assert fcg.forget_conversation_graph(conn, owner_id="u1", conversation_id=_C) == {}  # type: ignore[arg-type]
    assert conn.sql == []
    assert taken == []


def test_the_preview_off_postgres_or_unwired_is_empty() -> None:
    empty = fcg.ConversationGraphPreview(facts_removed=0, facts_kept=0)

    assert fcg.preview(None, owner_id="u1", conversation_id=_C) == empty

    class _Sqlite:
        dialect = _Dialect("sqlite")

    assert fcg.preview(_Sqlite(), owner_id="u1", conversation_id=_C) == empty  # type: ignore[arg-type]


def test_a_lock_timeout_or_deadlock_is_busy_and_nothing_else_is() -> None:
    class _Orig:
        def __init__(self, sqlstate: str) -> None:
            self.sqlstate = sqlstate

    from sqlalchemy.exc import OperationalError  # noqa: PLC0415

    def _error(sqlstate: str) -> OperationalError:
        return OperationalError("stmt", {}, _Orig(sqlstate))  # type: ignore[arg-type]

    assert fcg.is_busy(_error("55P03"))
    assert fcg.is_busy(_error("40P01"))
    assert not fcg.is_busy(_error("08006"))
