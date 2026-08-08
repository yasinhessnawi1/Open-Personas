"""``runs.error`` is a user surface and must be sanitised too (R9-097 remainder).

R9-097 fixed the chat path: a tier exhaustion no longer reaches a person as its
stringified exception, which names our providers, model ids, tier and error
classes. The run path kept writing ``str(exc)`` straight into ``runs.error``, and
that column is rendered on the run page. Same leak, different door.

The fix must not cost diagnosis, so these also pin that the FULL exception is
still logged, and that anything the mapper does not rewrite passes through
untouched (an unmapped exception often carries genuinely useful, already-safe
text, and blanket-genericising it would trade one bad experience for another).
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest
from persona.backends import AllModelsFailedError
from persona_api.background.run_worker import RunRegistry
from persona_api.db.community import create_community_schema, ensure_owner, make_community_engine
from persona_api.db.models import personas as personas_t
from persona_api.db.models import runs as runs_t
from persona_api.services.user_facing_errors import CAPACITY_BUSY_FREE_MESSAGE
from sqlalchemy import insert, select

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy import Engine

_OWNER = "user_leaky"
_PERSONA = "astrid"
_RUN = "run_error"

#: What the exhaustion actually stringifies to. Kept verbatim from production so
#: the assertions below are testing the real shape rather than a stand-in.
_ATTEMPTS = json.dumps(
    [{"provider": "openrouter", "model": "openai/gpt-oss-20b:free", "last_error": "RateLimit"}]
)


def _exhausted() -> AllModelsFailedError:
    return AllModelsFailedError(
        "every backend in MultiModelChatBackend exhausted",
        context={
            "tier": "frontier",
            "attempt_count": "2",
            "attempts_json": _ATTEMPTS,
            "final_error_class": "RateLimitError",
        },
    )


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = make_community_engine(tmp_path / "runs.db")
    create_community_schema(eng)
    ensure_owner(eng, owner_id=_OWNER, email="l@example.com")
    with eng.begin() as conn:
        conn.execute(insert(personas_t).values(id=_PERSONA, owner_id=_OWNER, yaml="name: Astrid"))
        conn.execute(
            insert(runs_t).values(
                id=_RUN, owner_id=_OWNER, persona_id=_PERSONA, task="t", status="running"
            )
        )
    yield eng
    eng.dispose()


class _ExplodingLoop:
    """A loop that fails the way production failed: the tier is exhausted."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def run(self, task: str, **_kw: object) -> object:  # noqa: ARG002
        raise self._exc


def _persisted_error(engine: Engine) -> str:
    with engine.begin() as conn:
        return str(conn.execute(select(runs_t.c.error).where(runs_t.c.id == _RUN)).scalar_one())


async def _run_to_completion(engine: Engine, exc: Exception) -> None:
    registry = RunRegistry(engine)
    handle = registry.start(
        run_id=_RUN,
        owner_id=_OWNER,
        loop=_ExplodingLoop(exc),  # type: ignore[arg-type]
        task_text="t",
    )
    assert handle.task is not None
    await asyncio.wait_for(handle.task, timeout=5)


@pytest.mark.asyncio
async def test_a_tier_exhaustion_does_not_reach_the_run_page_raw(engine: Engine) -> None:
    """THE regression: the run page must not print our vendor mix to the user."""
    await _run_to_completion(engine, _exhausted())

    stored = _persisted_error(engine)
    # No subscription row for this owner IS the free plan (the D-M4-9 default).
    assert stored == CAPACITY_BUSY_FREE_MESSAGE
    for leaked in ("openrouter", "gpt-oss-20b", "MultiModelChatBackend", "frontier"):
        assert leaked not in stored, f"{leaked!r} reached the user through runs.error"


@pytest.mark.asyncio
async def test_the_full_exception_is_still_logged(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sanitising the user's copy must not cost us the diagnostic record.

    loguru binds stderr at import, so ``caplog`` is unreliable here; the module's
    own logger is monkeypatched instead, which is what actually gets called.
    """
    from persona_api.background import run_worker

    logged: list[str] = []

    class _Recorder:
        def error(self, _template: str, **kw: object) -> None:
            logged.append(str(kw.get("err", "")))

        def __getattr__(self, _name: str) -> object:
            return lambda *_a, **_kw: None

    monkeypatch.setattr(run_worker, "_log", _Recorder())
    await _run_to_completion(engine, _exhausted())

    assert any("openrouter" in line for line in logged), (
        "the provider detail must survive in the LOG even though it leaves the UI"
    )


@pytest.mark.asyncio
async def test_an_unmapped_failure_is_not_genericised(engine: Engine) -> None:
    """Scope guard: only failures known to carry internal detail are rewritten.

    A mapper that swallowed every exception would replace useful, already-safe
    text with a vague sentence, which is the same defect pointing the other way.
    """
    await _run_to_completion(engine, ValueError("the workspace path does not exist"))

    assert _persisted_error(engine) == "the workspace path does not exist"
