"""Unit tests for :func:`acquire_voice_call_concurrency` (spec V1 T09).

Mirrors the persona-api ``imagegen.concurrency`` test surface
(D-V1-X-d15x-precedent-binding). The integration test in T11 exercises the
same helper end-to-end against a real Postgres + a real second connection
holding the lock; here the SQLAlchemy ``Connection.execute`` is faked so
the suite runs without infrastructure.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from persona.errors import PersonaError
from persona_voice.concurrency import (
    VoiceConcurrencyCappedError,
    acquire_voice_call_concurrency,
)


def _conn_returning(acquired: bool) -> MagicMock:
    """Build a fake SQLAlchemy Connection whose
    ``execute(...).first()`` returns a row with the given ``acquired``
    value, mimicking ``SELECT pg_try_advisory_xact_lock(...) AS acquired``."""
    conn = MagicMock()
    result = MagicMock()
    row = MagicMock()
    row.acquired = acquired
    result.first.return_value = row
    conn.execute.return_value = result
    return conn


# ---------- helper contract -------------------------------------------------


def test_acquire_yields_true_when_lock_was_available() -> None:
    conn = _conn_returning(acquired=True)
    with acquire_voice_call_concurrency(conn=conn, user_id="user_a") as acquired:
        assert acquired is True
    conn.execute.assert_called_once()
    # The first positional arg is the lock SQL; assert its body to keep the
    # advisory-lock primitive structurally pinned (catches accidental
    # downgrades to e.g. the blocking variant).
    sql_text = conn.execute.call_args.args[0]
    assert "pg_try_advisory_xact_lock" in str(sql_text)


def test_acquire_yields_false_when_lock_is_held_by_another_tx() -> None:
    conn = _conn_returning(acquired=False)
    with acquire_voice_call_concurrency(conn=conn, user_id="user_a") as acquired:
        assert acquired is False


def test_acquire_yields_false_when_no_row_returned() -> None:
    """Edge case: the SELECT returns no row (shouldn't happen in practice,
    but the helper must fail closed rather than raise here — the caller's
    own 429 surfacing catches it cleanly)."""
    conn = MagicMock()
    result = MagicMock()
    result.first.return_value = None
    conn.execute.return_value = result
    with acquire_voice_call_concurrency(conn=conn, user_id="user_a") as acquired:
        assert acquired is False


def test_user_id_is_passed_as_bound_parameter_not_interpolated() -> None:
    """SQL-injection safety: ``user_id`` MUST flow as a bound parameter so
    a malicious ``sub`` claim cannot embed SQL into the lock query."""
    conn = _conn_returning(acquired=True)
    with acquire_voice_call_concurrency(conn=conn, user_id="user_a'; DROP TABLE personas; --") as _:
        pass
    params = conn.execute.call_args.args[1]
    # R7 T7: the primitive generalized to N slots, so params also carry the slot
    # index now — but ``user_id`` is STILL a bound parameter (the injection-safety
    # intent this test guards), never string-interpolated into the lock SQL.
    assert params["user_id"] == "user_a'; DROP TABLE personas; --"
    assert params["slot"] == 0  # slots=1 → slot 0 (byte-preserves the cap-1 lock key)


def test_acquire_is_keyed_per_user_id() -> None:
    """Distinct user_ids → distinct executions. The advisory-lock key is
    derived inside Postgres via ``md5(user_id)`` so different users never
    contend at the same lock slot in the v0.1 traffic shape."""
    conn = _conn_returning(acquired=True)
    with acquire_voice_call_concurrency(conn=conn, user_id="user_a") as _:
        pass
    with acquire_voice_call_concurrency(conn=conn, user_id="user_b") as _:
        pass
    assert conn.execute.call_count == 2
    # user_id still keys the lock (slot index rides alongside post-R7 generalization).
    assert conn.execute.call_args_list[0].args[1]["user_id"] == "user_a"
    assert conn.execute.call_args_list[1].args[1]["user_id"] == "user_b"


# ---------- error class ----------------------------------------------------


def test_voice_concurrency_capped_error_is_persona_error() -> None:
    """The error subclass slots cleanly into the persona-core hierarchy so
    persona-voice's HTTP exception handler can map it to 429 by type."""
    err = VoiceConcurrencyCappedError(
        "already in flight",
        context={"user_id": "user_a"},
    )
    assert isinstance(err, PersonaError)
    assert "already in flight" in str(err)
    assert err.context["user_id"] == "user_a"


def test_voice_concurrency_capped_error_str_includes_context() -> None:
    err = VoiceConcurrencyCappedError(
        "busy",
        context={"user_id": "u", "live_session_id": "sess_other"},
    )
    rendered = str(err)
    assert "user_id=u" in rendered
    assert "live_session_id=sess_other" in rendered


# ---------- raise + 429 wiring ---------------------------------------------


def _proceed_or_raise(conn: object, user_id: str) -> None:
    """The documented caller pattern, extracted so the
    ``pytest.raises`` block contains a single statement (ruff PT012)."""
    with acquire_voice_call_concurrency(conn=conn, user_id=user_id) as acquired:  # type: ignore[arg-type]
        if not acquired:
            raise VoiceConcurrencyCappedError(
                "already in flight",
                context={"user_id": user_id},
            )


def test_canonical_caller_pattern_raises_on_lock_held() -> None:
    """The documented usage pattern: caller raises ``VoiceConcurrencyCappedError``
    when ``acquired is False``. Codifies the contract so the surface stays
    stable as T06/T04 integrations land."""
    conn = _conn_returning(acquired=False)
    with pytest.raises(VoiceConcurrencyCappedError):
        _proceed_or_raise(conn, "u")


def test_canonical_caller_pattern_proceeds_on_lock_acquired() -> None:
    """Acquired path: the caller proceeds inside the with-block."""
    conn = _conn_returning(acquired=True)
    proceeded = False
    with acquire_voice_call_concurrency(conn=conn, user_id="u") as acquired:
        if acquired:
            proceeded = True
    assert proceeded is True
