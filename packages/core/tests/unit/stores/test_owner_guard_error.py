"""A guard refusal says what happened and carries none of the memory (R9-284 review).

The write the guard refuses is a memory, and SQLAlchemy puts a failed statement's parameters in
``str(exc)``. A background job stores ``str(exc)`` in ``jobs.last_error``, so a refusal that
fired because the persona had just been erased wrote the persona's memory back into the database
afterwards. It is a domain error now, raised ``from None``, and it is permanent: the owner is gone
and retrying cannot bring it back.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from persona.errors import MemoryOwnerMissingError, PermanentJobError
from persona.schema.chunks import PersonaChunk
from persona.stores.postgres import EMBEDDING_DIM, PostgresBackend
from sqlalchemy.exc import IntegrityError

_SECRET = "my dog is called Balto"
_GUARD = "memory_chunks_owner_guard: the owner of this episodic row does not exist"


class _Embedder:
    model_name = "unit"

    @property
    def dimension(self) -> int:
        return EMBEDDING_DIM

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * EMBEDDING_DIM for _ in texts]


def _integrity_error(*, sqlstate: str, message: str) -> IntegrityError:
    orig = Exception(message)
    orig.sqlstate = sqlstate  # type: ignore[attr-defined]
    orig.diag = SimpleNamespace(  # type: ignore[attr-defined]
        message_primary=message, constraint_name=None
    )
    return IntegrityError("INSERT INTO memory_chunks ...", {"text": _SECRET}, orig)


class _Connection:
    def __init__(self, error: Exception) -> None:
        self._error = error

    def execute(self, *_args: Any, **_kwargs: Any) -> None:  # noqa: ANN401
        raise self._error


class _Begin:
    def __init__(self, error: Exception) -> None:
        self._error = error

    def __enter__(self) -> _Connection:
        return _Connection(self._error)

    def __exit__(self, *_exc: object) -> bool:
        return False


class _Engine:
    def __init__(self, error: Exception) -> None:
        self._error = error

    def begin(self) -> _Begin:
        return _Begin(self._error)


def _backend(error: Exception) -> PostgresBackend:
    return PostgresBackend(engine=_Engine(error), embedder=_Embedder())  # type: ignore[arg-type]


def _chunk() -> PersonaChunk:
    return PersonaChunk(id="c1", text=_SECRET, created_at=datetime(2026, 11, 8, tzinfo=UTC))


def _upsert(backend: PostgresBackend) -> None:
    backend.upsert(persona_id="persona_gone", store_kind="episodic", chunks=[_chunk()])


def test_the_refusal_becomes_a_domain_error_without_the_memory() -> None:
    backend = _backend(_integrity_error(sqlstate="23503", message=_GUARD))

    with pytest.raises(MemoryOwnerMissingError) as caught:
        _upsert(backend)

    assert _SECRET not in str(caught.value)
    assert caught.value.context == {"store_kind": "episodic"}


def test_the_original_error_is_not_chained_where_a_traceback_would_print_it() -> None:
    backend = _backend(_integrity_error(sqlstate="23503", message=_GUARD))

    with pytest.raises(MemoryOwnerMissingError) as caught:
        _upsert(backend)

    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True


def test_a_job_that_hits_it_is_not_retried() -> None:
    assert issubclass(MemoryOwnerMissingError, PermanentJobError)


@pytest.mark.parametrize(
    ("sqlstate", "message"),
    [
        ("23503", "some other foreign key was violated"),
        ("23514", _GUARD),
    ],
)
def test_any_other_integrity_error_is_left_alone(sqlstate: str, message: str) -> None:
    backend = _backend(_integrity_error(sqlstate=sqlstate, message=message))

    with pytest.raises(IntegrityError):
        _upsert(backend)
