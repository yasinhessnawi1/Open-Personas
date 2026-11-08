"""A background job that outlives its persona stops, and leaves nothing behind (R9-284).

Episodic consolidation is queued with a 15 minute idle delay after every turn, so deleting a
persona soon after talking to it always leaves one to run. On the community edition, which
connects as a superuser, that pass read the persona's leftover memory, spent money on the
summariser and merged the result into the shared graph, all for a persona that no longer exists.
The database now refuses its memory writes (migration 062). These stop the work before it costs
anything.

An avatar job has the opposite race: the persona can go while the image is being generated, and
the job then writes the bytes back into a folder the delete has just removed.
"""

# ruff: noqa: ANN401, ARG002, ARG005 - protocol-required arguments the fakes do not use
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from persona.stores.engine import EpisodicConsolidationReport
from persona_api.imagegen.service import ImagegenAvatarGenerator
from persona_api.jobs.handlers import avatar as avatar_module
from persona_api.jobs.handlers import episodic_consolidation as episodic_module
from persona_api.jobs.handlers.avatar import (
    AvatarGenerationHandler,
    AvatarGenerationPayload,
    AvatarResult,
)
from persona_api.jobs.handlers.episodic_consolidation import (
    EpisodicConsolidationHandler,
    EpisodicConsolidationJobPayload,
)
from persona_api.storage import LocalFileStorage
from sqlalchemy import Engine, create_engine, text

if TYPE_CHECKING:
    from collections.abc import Iterator

_OWNER = "u_jobs"
_PERSONA = "persona_f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0"
_PAYLOAD = EpisodicConsolidationJobPayload(persona_id=_PERSONA, watermark_bucket="1")
_RESULT = AvatarResult(avatar_url="uploads/new.png", cost_micros=250, provider="fake")


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = create_engine("sqlite+pysqlite:///:memory:")
    with eng.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE personas (id TEXT PRIMARY KEY, owner_id TEXT, yaml TEXT, "
                "avatar_url TEXT, avatar_source TEXT, updated_at TEXT)"
            )
        )
        conn.execute(
            text("INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :o, 'x: 1')"),
            {"p": _PERSONA, "o": _OWNER},
        )
    yield eng
    eng.dispose()


def _delete_persona(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM personas WHERE id = :p"), {"p": _PERSONA})


class _Context:
    def __init__(self, engine: Engine, owner_id: str = _OWNER) -> None:
        self._engine = engine
        self._owner_id = owner_id
        self.metered: list[dict[str, Any]] = []
        self.fail_connections_after: int | None = None
        self.connections = 0

    @property
    def owner_id(self) -> str:
        return self._owner_id

    @property
    def job_id(self) -> str:
        return "job-1"

    @contextmanager
    def connection(self) -> Iterator[Any]:
        self.connections += 1
        limit = self.fail_connections_after
        if limit is not None and self.connections > limit:
            raise ConnectionError("the database went away")
        with self._engine.begin() as conn:
            yield conn

    def meter(self, *, amount_micros: int, kind: str, detail: Any = None) -> None:
        self.metered.append({"amount_micros": amount_micros, "kind": kind})


def _report() -> EpisodicConsolidationReport:
    return EpisodicConsolidationReport(
        persona_id=_PERSONA,
        chunks_considered=1,
        windows_formed=1,
        windows_deferred_open=0,
        gists_written=1,
        candidates_emitted=1,
    )


class _Engine:
    def __init__(self, on_run: Any = None) -> None:
        self.runs: list[tuple[str, str]] = []
        self._on_run = on_run

    async def run(self, owner_id: str, persona_id: str) -> EpisodicConsolidationReport:
        self.runs.append((owner_id, persona_id))
        if self._on_run is not None:
            self._on_run()
        return _report()


class _Refresher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, owner_id: str, persona_id: str) -> None:
        self.calls.append((owner_id, persona_id))


@pytest.fixture
def billed(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        episodic_module, "bill_background_llm", lambda **kwargs: calls.append(kwargs)
    )
    return calls


# --- episodic consolidation ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_consolidation_for_a_deleted_persona_does_nothing_and_costs_nothing(
    engine: Engine, billed: list[dict[str, Any]]
) -> None:
    _delete_persona(engine)
    runner, refresher, context = _Engine(), _Refresher(), _Context(engine)

    await EpisodicConsolidationHandler(engine=runner, core_refresher=refresher).handle(
        _PAYLOAD,
        context,  # type: ignore[arg-type]
    )

    assert runner.runs == []
    assert refresher.calls == []
    assert billed == []
    assert context.metered == []


@pytest.mark.asyncio
async def test_another_owners_persona_counts_as_deleted(
    engine: Engine, billed: list[dict[str, Any]]
) -> None:
    """The community engine has no row level security to narrow this lookup."""
    runner, context = _Engine(), _Context(engine, owner_id="someone_else")

    await EpisodicConsolidationHandler(engine=runner).handle(
        _PAYLOAD,
        context,  # type: ignore[arg-type]
    )

    assert runner.runs == []
    assert billed == []


@pytest.mark.asyncio
async def test_a_live_persona_runs_as_before(engine: Engine, billed: list[dict[str, Any]]) -> None:
    runner, refresher, context = _Engine(), _Refresher(), _Context(engine)

    await EpisodicConsolidationHandler(engine=runner, core_refresher=refresher).handle(
        _PAYLOAD,
        context,  # type: ignore[arg-type]
    )

    assert runner.runs == [(_OWNER, _PERSONA)]
    assert refresher.calls == [(_OWNER, _PERSONA)]
    assert len(billed) == 1
    assert len(context.metered) == 1


@pytest.mark.asyncio
async def test_the_core_block_is_not_refreshed_once_the_persona_has_gone(
    engine: Engine, billed: list[dict[str, Any]]
) -> None:
    """The refresh is a model call of its own, and the engine pass takes minutes."""
    runner, refresher = _Engine(on_run=lambda: _delete_persona(engine)), _Refresher()

    await EpisodicConsolidationHandler(engine=runner, core_refresher=refresher).handle(
        _PAYLOAD,
        _Context(engine),  # type: ignore[arg-type]
    )

    assert len(runner.runs) == 1
    assert refresher.calls == []
    assert len(billed) == 1, "the engine pass really happened and really cost money"


@pytest.mark.asyncio
async def test_a_failed_check_before_the_refresh_does_not_fail_the_job(
    engine: Engine, billed: list[dict[str, Any]]
) -> None:
    """A core refresh must never fail the job, and that includes deciding whether to run it."""
    refresher, context = _Refresher(), _Context(engine)
    context.fail_connections_after = 1

    await EpisodicConsolidationHandler(engine=_Engine(), core_refresher=refresher).handle(
        _PAYLOAD,
        context,  # type: ignore[arg-type]
    )

    assert refresher.calls == []
    assert len(billed) == 1


# --- avatar generation ---------------------------------------------------------------------


class _Generator:
    def __init__(self, on_generate: Any = None, *, discard_raises: Exception | None = None) -> None:
        self._on_generate = on_generate
        self._discard_raises = discard_raises
        self.discarded: list[tuple[str, str]] = []

    async def generate(
        self, *, persona_id: str, owner_id: str, yaml_str: str, billing_key: str
    ) -> AvatarResult | None:
        if self._on_generate is not None:
            self._on_generate()
        return _RESULT

    def discard(self, *, persona_id: str, owner_id: str) -> None:
        self.discarded.append((persona_id, owner_id))
        if self._discard_raises is not None:
            raise self._discard_raises


@pytest.fixture
def announced(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    written: list[str] = []
    monkeypatch.setattr(
        avatar_module.persona_service,
        "write_persona_ready",
        lambda conn, persona_id: written.append(persona_id),
    )
    return written


@pytest.mark.asyncio
async def test_a_persona_deleted_mid_generation_has_the_new_bytes_removed(
    engine: Engine, announced: list[str]
) -> None:
    generator = _Generator(on_generate=lambda: _delete_persona(engine))

    await AvatarGenerationHandler(generator=generator).handle(
        AvatarGenerationPayload(persona_id=_PERSONA),
        _Context(engine),  # type: ignore[arg-type]
    )

    assert generator.discarded == [(_PERSONA, _OWNER)]
    assert announced == []


@pytest.mark.asyncio
async def test_losing_the_race_to_another_delivery_keeps_the_files(
    engine: Engine, announced: list[str]
) -> None:
    """The persona is still here and its avatar is set, so the files are in use."""

    def another_delivery_won() -> None:
        with engine.begin() as conn:
            conn.execute(text("UPDATE personas SET avatar_url = 'uploads/winner.png'"))

    generator = _Generator(on_generate=another_delivery_won)

    await AvatarGenerationHandler(generator=generator).handle(
        AvatarGenerationPayload(persona_id=_PERSONA),
        _Context(engine),  # type: ignore[arg-type]
    )

    assert generator.discarded == []
    assert announced == []


@pytest.mark.asyncio
async def test_an_ordinary_generation_discards_nothing(
    engine: Engine, announced: list[str]
) -> None:
    generator = _Generator()

    await AvatarGenerationHandler(generator=generator).handle(
        AvatarGenerationPayload(persona_id=_PERSONA),
        _Context(engine),  # type: ignore[arg-type]
    )

    assert generator.discarded == []
    assert announced == [_PERSONA]


@pytest.mark.asyncio
@pytest.mark.usefixtures("announced")
async def test_a_failed_discard_does_not_fail_the_job(engine: Engine) -> None:
    """A retry would find no persona and do nothing, so failing the job gains nothing."""
    generator = _Generator(
        on_generate=lambda: _delete_persona(engine), discard_raises=PermissionError("in use")
    )

    await AvatarGenerationHandler(generator=generator).handle(
        AvatarGenerationPayload(persona_id=_PERSONA),
        _Context(engine),  # type: ignore[arg-type]
    )

    assert generator.discarded == [(_PERSONA, _OWNER)]


def test_the_real_generator_removes_that_persona_and_nothing_else(tmp_path: Path) -> None:
    storage = LocalFileStorage(tmp_path)
    storage.put(f"{_OWNER}/{_PERSONA}/uploads/new.png", b"x")
    storage.put(f"{_OWNER}/{_PERSONA}/uploads/new.png.f5.json", b"{}")
    other = "persona_a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0"
    storage.put(f"{_OWNER}/{other}/uploads/keep.png", b"k")
    generator = ImagegenAvatarGenerator(backend=object(), file_storage=storage)  # type: ignore[arg-type]

    generator.discard(persona_id=_PERSONA, owner_id=_OWNER)

    assert not (tmp_path / _OWNER / _PERSONA).exists()
    assert (tmp_path / _OWNER / other / "uploads" / "keep.png").exists()
