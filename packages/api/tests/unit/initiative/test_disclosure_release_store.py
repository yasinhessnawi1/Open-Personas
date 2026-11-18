"""D-ORCH-11-18-u: the release and the re-block on the fact row, without a database.

A release covers the disclosures recorded so far and nothing recorded after it: the record
bumps ``disclosure_seq`` and the release copies it into ``released_seq``. The release only
applies to the count the user saw (``version``), so a disclosure recorded between the read and
the click is never released unseen. Each change writes one neutral audit row in the same
transaction: ``conversation.setting_changed``, the conversation, and the persona id, nothing
else (no state, no counter), so the audit carries no disclosure marker. The SQL is proven on
Postgres in ``tests/integration/test_wellbeing_disclosure_store.py``.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from persona_api.initiative import disclosures
from persona_api.initiative.disclosures import (
    SETTING_CHANGED_ACTION,
    DisclosureState,
    PgDisclosureStore,
    ReleaseOutcome,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

if TYPE_CHECKING:
    from collections.abc import Iterator

_PG = postgresql.dialect()  # type: ignore[no-untyped-call]
_AT = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


class _Row:
    def __init__(self, **values: Any) -> None:  # noqa: ANN401
        self.__dict__.update(values)
        self._values = tuple(values.values())

    def __getitem__(self, index: int) -> Any:  # noqa: ANN401
        return self._values[index]


class _Result:
    def __init__(self, rows: list[_Row]) -> None:
        self._rows = rows

    @property
    def rowcount(self) -> int:
        raise AssertionError("the store must not trust rowcount")

    def first(self) -> _Row | None:
        return self._rows[0] if self._rows else None

    def all(self) -> list[_Row]:
        return self._rows


class _Script:
    """Answers each statement with the next scripted rows; keeps every statement."""

    def __init__(self, *answers: list[_Row]) -> None:
        self._answers = list(answers)
        self.statements: list[Any] = []

    def execute(self, stmt: Any) -> _Result:  # noqa: ANN401
        self.statements.append(stmt)
        return _Result(self._answers.pop(0) if self._answers else [])

    def sql(self, index: int) -> str:
        return str(self.statements[index].compile(dialect=_PG)).upper()

    def params(self, index: int) -> dict[str, Any]:
        return dict(self.statements[index].compile(dialect=_PG).params)


def _scripted(monkeypatch: pytest.MonkeyPatch, *answers: list[_Row]) -> _Script:
    script = _Script(*answers)

    @contextmanager
    def connection(_engine: Any, _owner_id: str) -> Iterator[_Script]:  # noqa: ANN401
        yield script

    monkeypatch.setattr(disclosures, "rls_connection", connection)
    return script


_PERSONA = [_Row(id="pers")]


def _store() -> PgDisclosureStore:
    return PgDisclosureStore(object())  # type: ignore[arg-type]


def _audit_rows(script: _Script) -> list[dict[str, Any]]:
    return [
        script.params(i)
        for i in range(len(script.statements))
        if script.sql(i).startswith("INSERT INTO AUDIT_LOG")
    ]


def test_a_recorded_disclosure_bumps_the_count_of_an_existing_fact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutant 3: DO NOTHING here would leave a release covering a later disclosure."""
    script = _scripted(monkeypatch, _PERSONA)
    assert _store().record("u", "pers", "conv") is True
    insert = script.sql(1)
    assert insert.startswith("INSERT INTO WELLBEING_DISCLOSURES")
    assert "ON CONFLICT (OWNER_ID, INTERACTION_ID) DO UPDATE SET DISCLOSURE_SEQ" in insert
    assert "WELLBEING_DISCLOSURES.DISCLOSURE_SEQ +" in insert


def test_a_release_covers_the_count_the_user_saw(monkeypatch: pytest.MonkeyPatch) -> None:
    script = _scripted(monkeypatch, _PERSONA, [_Row(disclosure_seq=2, released_seq=None)])
    outcome = _store().release("u", "pers", "conv", version=2, at=_AT)
    assert outcome is ReleaseOutcome.CHANGED
    assert "FOR KEY SHARE" in script.sql(0), "the persona row is held until commit"
    assert "FOR UPDATE" in script.sql(1), "the fact row is locked against a concurrent record"
    update = script.sql(2)
    assert update.startswith("UPDATE WELLBEING_DISCLOSURES SET")
    assert "RELEASED_SEQ=WELLBEING_DISCLOSURES.DISCLOSURE_SEQ" in update.replace(" ", "")
    assert _AT in script.params(2).values()


def test_a_release_of_a_count_the_user_did_not_see_is_a_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A disclosure recorded between the read and the click is never released unseen."""
    script = _scripted(monkeypatch, _PERSONA, [_Row(disclosure_seq=3, released_seq=None)])
    assert _store().release("u", "pers", "conv", version=2, at=_AT) is ReleaseOutcome.CONFLICT
    assert len(script.statements) == 2, "nothing written"


def test_a_release_held_only_by_a_tagged_node_writes_a_released_fact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No fact yet (a merge tagged the node): the row is born released, at one disclosure."""
    script = _scripted(monkeypatch, _PERSONA, [], [_Row(interaction_id="conv")])
    assert _store().release("u", "pers", "conv", version=0, at=_AT) is ReleaseOutcome.CHANGED
    insert = script.sql(2)
    assert insert.startswith("INSERT INTO WELLBEING_DISCLOSURES")
    assert "ON CONFLICT DO NOTHING" in insert
    params = script.params(2)
    assert params["disclosure_seq"] == 1
    assert params["released_seq"] == 1
    assert params["released_at"] == _AT


def test_a_fact_landing_while_the_release_inserts_is_a_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _scripted(monkeypatch, _PERSONA, [], [])
    assert _store().release("u", "pers", "conv", version=0, at=_AT) is ReleaseOutcome.CONFLICT
    assert _audit_rows(script) == []


def test_a_release_given_again_restamps_its_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """PR #44 review MEDIUM-1: the count is covered, yet a tagged write after the release
    (the ``written_at`` net) holds the conversation. The user releases again with what they
    saw: that is a new release, so its time moves and it is audited; it is never UNCHANGED.
    """
    script = _scripted(monkeypatch, _PERSONA, [_Row(disclosure_seq=2, released_seq=2)])
    assert _store().release("u", "pers", "conv", version=2, at=_AT) is ReleaseOutcome.CHANGED
    update = script.sql(2)
    assert update.startswith("UPDATE WELLBEING_DISCLOSURES SET")
    assert _AT in script.params(2).values()
    assert len(_audit_rows(script)) == 1


def test_a_release_for_a_persona_that_is_gone_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _scripted(monkeypatch, [])
    assert _store().release("u", "gone", "conv", version=1, at=_AT) is ReleaseOutcome.GONE
    assert len(script.statements) == 1


def test_losing_the_release_race_with_an_erasure_is_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    @contextmanager
    def connection(_engine: Any, _owner_id: str) -> Iterator[Any]:  # noqa: ANN401
        raise IntegrityError("INSERT", {}, Exception("fk"))
        yield  # pragma: no cover

    monkeypatch.setattr(disclosures, "rls_connection", connection)
    assert _store().release("u", "p", "conv", version=1, at=_AT) is ReleaseOutcome.GONE


def test_a_re_block_clears_the_release(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutant 10: a re-block that leaves ``released_seq`` keeps the conversation released."""
    script = _scripted(monkeypatch, [_Row(interaction_id="conv")])
    assert _store().hold("u", "pers", "conv") is ReleaseOutcome.CHANGED
    update = script.sql(0)
    assert update.startswith("UPDATE WELLBEING_DISCLOSURES SET")
    assert "RELEASED_SEQ IS NOT NULL" in update
    params = script.params(0)
    assert params["released_seq"] is None
    assert params["released_at"] is None


def test_a_re_block_of_nothing_released_changes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    script = _scripted(monkeypatch, [])
    assert _store().hold("u", "pers", "conv") is ReleaseOutcome.UNCHANGED
    assert _audit_rows(script) == []


@pytest.mark.parametrize("change", ["release", "hold"])
def test_every_change_writes_one_neutral_audit_row(
    monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """Mutant 12: no state and no counter in the audit, so it marks no disclosure."""
    if change == "release":
        script = _scripted(monkeypatch, _PERSONA, [_Row(disclosure_seq=1, released_seq=None)])
        _store().release("u", "pers", "conv", version=1, at=_AT)
    else:
        script = _scripted(monkeypatch, [_Row(interaction_id="conv")])
        _store().hold("u", "pers", "conv")
    rows = _audit_rows(script)
    assert len(rows) == 1
    audit = rows[0]
    assert audit["user_id"] == "u"
    assert audit["action"] == SETTING_CHANGED_ACTION == "conversation.setting_changed"
    assert audit["target"] == "conv"
    assert audit["metadata"] == {"persona_id": "pers"}
    assert str(audit["id"]).startswith("audit_")
    assert script.sql(len(script.statements) - 1).startswith("INSERT INTO AUDIT_LOG"), (
        "written last, inside the same transaction as the change"
    )


def test_the_states_read_one_select_and_no_refs_touch_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _scripted(
        monkeypatch,
        [
            _Row(
                interaction_id="a",
                disclosure_seq=2,
                released_seq=2,
                released_at=_AT,
                created_at=_AT,
            ),
            _Row(
                interaction_id="b",
                disclosure_seq=3,
                released_seq=2,
                released_at=_AT,
                created_at=_AT,
            ),
        ],
    )
    assert _store().states("u", []) == {}
    assert script.statements == []
    got = _store().states("u", ["b", "a", "a"])
    assert got == {
        "a": DisclosureState(disclosure_seq=2, released_seq=2, released_at=_AT, created_at=_AT),
        "b": DisclosureState(disclosure_seq=3, released_seq=2, released_at=_AT, created_at=_AT),
    }
    assert len(script.statements) == 1


@pytest.mark.parametrize(
    ("seq", "released", "at", "in_force"),
    [
        (2, 2, _AT, True),
        (3, 2, _AT, False),  # a disclosure recorded after the release (mutant 4: >=)
        (2, None, None, False),
        (2, 2, None, False),  # never without its time
    ],
)
def test_a_release_is_in_force_only_for_the_disclosures_it_covers(
    seq: int, released: int | None, at: datetime | None, in_force: bool
) -> None:
    state = DisclosureState(disclosure_seq=seq, released_seq=released, released_at=at)
    assert state.release_in_force is in_force
