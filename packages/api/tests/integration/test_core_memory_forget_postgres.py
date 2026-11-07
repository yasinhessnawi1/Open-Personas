"""A forget reaches the core block, on the production transport (R9-209).

The unit tests pin the logic over a model. These pin what only a real database can. That moving
the head to a survivor is legal under the head index, because the doomed head has to lose its NULL
first. That the rewrite is one transaction, so a failure leaves the chain as it was. That the path
a person really takes, deleting a conversation with forget on, gets there. And that a refresh
really in flight on another thread, holding sources across a model call, cannot land a block
citing what a forget just deleted.
"""

from __future__ import annotations

import asyncio
import os
import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from persona.audit import MemoryAuditLogger
from persona.cli.main import app
from persona.recall.core_memory import (
    CoreBlock,
    core_block_to_chunk,
    read_core_block,
    refresh_core_block,
)
from persona.schema.chunks import ChunkProvenance, PersonaChunk, WriteSource
from persona.stores.core_memory import CORE_MEMORY_KIND, CoreMemoryStore, core_logical_id
from persona.stores.episodic import EpisodicStore
from persona.stores.postgres import PostgresBackend
from persona.stores.versioning import validate_chain
from persona_api.services import chat_service
from sqlalchemy import text
from typer.testing import CliRunner

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine
    from tests.conftest import HashEmbedder384

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 11, 6, 9, 0, tzinfo=UTC)
_PERSONA = "p1"
_CHAIN = core_logical_id(_PERSONA)
_TIMEOUT_S = 30


@pytest.fixture
def seeded(pg_engine: Engine) -> Engine:
    with pg_engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email) VALUES ('u1', 'u1@example.com')"))
        conn.execute(
            text("INSERT INTO personas (id, owner_id, yaml) VALUES ('p1', 'u1', 'name: p1')")
        )
    return pg_engine


def _stores(
    engine: Engine, embedder: HashEmbedder384
) -> tuple[CoreMemoryStore, EpisodicStore, MemoryAuditLogger]:
    backend = PostgresBackend(engine=engine, embedder=embedder)
    audit = MemoryAuditLogger()
    return (
        CoreMemoryStore(backend=backend, audit_logger=audit),
        EpisodicStore(backend=backend, audit_logger=audit),
        audit,
    )


def _block(store: CoreMemoryStore, body: str, *sources: str) -> None:
    chunk = core_block_to_chunk(CoreBlock(text=body, source_ids=sources), _PERSONA, now=_NOW)
    store.write(_PERSONA, [chunk], source=WriteSource.SYSTEM)


def _evidence(store: EpisodicStore, chunk_id: str, **metadata: str) -> PersonaChunk:
    chunk = PersonaChunk(
        id=chunk_id,
        text=f"evidence {chunk_id}",
        metadata=metadata,
        created_at=_NOW,
        provenance=ChunkProvenance(source=WriteSource.SYSTEM, logical_id=chunk_id, written_at=_NOW),
    )
    store.write(_PERSONA, [chunk], source=WriteSource.SYSTEM)
    return chunk


def _as_read(core: CoreMemoryStore) -> dict[str, tuple[int, str | None]]:
    """The chain as a forget would have read it, for a direct call to ``excise_versions``."""
    return {
        c.id: (c.provenance.version, c.provenance.superseded_by)
        for c in core.history(_PERSONA, _CHAIN)
        if c.provenance is not None
    }


def _core_rows(engine: Engine) -> list[tuple[Any, ...]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT text, version, superseded_by IS NULL FROM memory_chunks "
                "WHERE kind = 'core_memory' ORDER BY version"
            )
        )
        return [tuple(r) for r in rows]


@pytest.mark.parametrize(
    ("forgotten", "left"),
    [
        ("a", [("second", 1, False), ("third", 2, True)]),
        ("b", [("first", 1, False), ("third", 2, True)]),
        ("c", [("first", 1, False), ("second", 2, True)]),
    ],
    ids=["oldest", "middle", "head"],
)
def test_the_chain_is_valid_under_the_real_index_whichever_version_went(
    seeded: Engine,
    embedder: HashEmbedder384,
    forgotten: str,
    left: list[tuple[Any, ...]],
) -> None:
    core, episodic, _ = _stores(seeded, embedder)
    for name, cited in (("first", "a"), ("second", "b"), ("third", "c")):
        _block(core, name, cited)
        _evidence(episodic, cited)

    episodic.remove_documents(_PERSONA, [forgotten])

    assert _core_rows(seeded) == left
    validate_chain(core.history(_PERSONA, _CHAIN))


def test_the_rewrite_is_one_transaction(seeded: Engine, embedder: HashEmbedder384) -> None:
    """A link that cannot be written rolls the delete back, so no row went without its fix."""
    core, _, _ = _stores(seeded, embedder)
    _block(core, "first", "a")
    _block(core, "second", "b")
    head = core.current(_PERSONA)
    assert head is not None
    backend = PostgresBackend(engine=seeded, embedder=embedder)

    with pytest.raises(Exception, match="out of range|integer"):
        backend.excise_versions(
            persona_id=_PERSONA,
            store_kind=CORE_MEMORY_KIND,
            logical_id=_CHAIN,
            expected=_as_read(core),
            doomed_ids=[head.id],
            links={},
            versions={head.id: 1, "another": 2**40},
        )

    assert len(_core_rows(seeded)) == 2


def _conversation(engine: Engine, conversation_id: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO conversations (id, owner_id, persona_id) VALUES (:id, 'u1', :persona)"
            ),
            {"id": conversation_id, "persona": _PERSONA},
        )


def test_deleting_a_conversation_with_forget_on_reaches_the_block(
    seeded: Engine, embedder: HashEmbedder384
) -> None:
    """The path a person takes: K11's door, from the route's service, on a real database."""
    core, episodic, audit = _stores(seeded, embedder)
    _conversation(seeded, "c1")
    _evidence(episodic, "kept")
    _block(core, "older", "kept")
    _evidence(episodic, "said-in-c1", conversation_id="c1")
    _block(core, "newer and citing it", "said-in-c1", "kept")

    chat_service.delete_conversation(
        rls_engine=seeded,
        conversation_id="c1",
        forget_memory=True,
        persona_id=_PERSONA,
        episodic_store=episodic,
    )

    shown = read_core_block(core, _PERSONA)
    assert shown is not None
    assert shown.text == "older", "the newest summary citing nothing forgotten"
    assert _core_rows(seeded) == [("older", 1, True)]
    assert core.orphaned_citations(_PERSONA) == []
    event = audit.events[-1]
    assert event.store == CORE_MEMORY_KIND
    assert "newer" not in event.model_dump_json()


class _HeldModel:
    """A model call that stays open until the test lets it finish."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.finish = threading.Event()

    async def summarize(self, content: str, *, target_tokens: int) -> str:
        _ = (content, target_tokens)
        self.started.set()
        await asyncio.to_thread(self.finish.wait, _TIMEOUT_S)
        return "a summary written from something just forgotten"


def test_a_refresh_in_flight_cannot_land_a_block_citing_what_a_forget_deleted(
    seeded: Engine, embedder: HashEmbedder384
) -> None:
    core, episodic, _ = _stores(seeded, embedder)
    source = _evidence(episodic, "a")
    model = _HeldModel()
    results: list[CoreBlock | None] = []

    def refresh() -> None:
        results.append(
            asyncio.run(
                refresh_core_block(
                    _PERSONA,
                    summarizer=model,
                    sources=[source],
                    store=core,
                    target_tokens=50,
                    now=_NOW,
                )
            )
        )

    thread = threading.Thread(target=refresh)
    thread.start()
    assert model.started.wait(_TIMEOUT_S)

    episodic.remove_documents(_PERSONA, ["a"])
    model.finish.set()
    thread.join(_TIMEOUT_S)

    assert not thread.is_alive()
    assert results == [None]
    assert _core_rows(seeded) == []


def _sweep(*args: str) -> Any:  # noqa: ANN401 - typer's Result
    return CliRunner().invoke(
        app,
        [
            "repair",
            "--all-personas",
            "--database-url",
            os.environ["DATABASE_URL"],
            "--forgotten-evidence",
            *args,
        ],
    )


def test_the_operator_sweep_finds_it_deletes_it_and_says_so_in_the_database(
    seeded: Engine, embedder: HashEmbedder384
) -> None:
    core, episodic, _ = _stores(seeded, embedder)
    _evidence(episodic, "kept")
    _block(core, "clean", "kept")
    _block(core, "restored from before the rule", "kept", "long-gone")

    dry = _sweep()
    assert dry.exit_code == 1, dry.output
    assert "p1: 1 forgotten sources are cited" in dry.output
    assert len(_core_rows(seeded)) == 2, "a dry run changed something"

    applied = _sweep("--apply")
    assert applied.exit_code == 0, applied.output
    assert _core_rows(seeded) == [("clean", 1, True)]
    with seeded.connect() as conn:
        audited = conn.execute(
            text(
                "SELECT count(*) FROM store_audit_events "
                "WHERE action = 'remove_documents' AND store = 'core_memory'"
            )
        ).scalar()
    assert audited == 1
    assert _sweep().exit_code == 0


def test_the_sweep_on_a_clean_database_is_calm(seeded: Engine, embedder: HashEmbedder384) -> None:
    core, episodic, _ = _stores(seeded, embedder)
    _evidence(episodic, "kept")
    _block(core, "clean", "kept")
    result = _sweep()
    assert result.exit_code == 0, result.output
    assert "cites anything forgotten" in result.output


def test_the_sweep_needs_no_model(seeded: Engine, embedder: HashEmbedder384) -> None:
    """The command's transport refuses to embed, so any model use would fail it."""
    from persona.cli.repair_cmd import NoEmbedder

    core, _, _ = _stores(seeded, embedder)
    _block(core, "restored", "long-gone")
    refusing = CoreMemoryStore(
        backend=PostgresBackend(engine=seeded, embedder=NoEmbedder()),
        audit_logger=MemoryAuditLogger(),
    )

    assert refusing.forget_cited(_PERSONA, refusing.orphaned_citations(_PERSONA)) == 1
    assert _core_rows(seeded) == []


# --- the round 2 review ---------------------------------------------------------------


class _RefreshLandsInsideTheForget(PostgresBackend):
    """Runs ``hook`` after the forget has read the chain and before it excises (R2-M1 b)."""

    hook: Any = None

    def excise_versions(self, **kwargs: Any) -> None:  # noqa: ANN401 - the transport's keywords
        hook, self.hook = self.hook, None
        if hook is not None:
            hook()
        super().excise_versions(**kwargs)


def test_a_forget_planned_on_a_stale_read_finishes_on_the_real_index(
    seeded: Engine, embedder: HashEmbedder384
) -> None:
    racing = _RefreshLandsInsideTheForget(engine=seeded, embedder=embedder)
    core = CoreMemoryStore(backend=racing, audit_logger=MemoryAuditLogger())
    refresher, episodic, _ = _stores(seeded, embedder)
    _evidence(episodic, "kept")
    _block(core, "first", "kept")
    _block(core, "second", "gone")
    racing.hook = lambda: _block(refresher, "a newer block", "kept")

    assert core.forget_cited(_PERSONA, ["gone"]) == 1  # was a raw IntegrityError: a 500

    assert _core_rows(seeded) == [("first", 1, False), ("a newer block", 2, True)]
    assert core.orphaned_citations(_PERSONA) == []


def test_a_stale_excise_is_the_domain_error_and_changes_nothing(
    seeded: Engine, embedder: HashEmbedder384
) -> None:
    from persona.errors import ChainConflictError

    core, _, _ = _stores(seeded, embedder)
    _block(core, "first", "a")
    _block(core, "second", "b")
    first = next(c for c in core.history(_PERSONA, _CHAIN) if c.text == "first")
    before = _core_rows(seeded)

    with pytest.raises(ChainConflictError) as caught:
        PostgresBackend(engine=seeded, embedder=embedder).excise_versions(
            persona_id=_PERSONA,
            store_kind=CORE_MEMORY_KIND,
            logical_id=_CHAIN,
            expected=_as_read(core),  # the plan is current, so only the head index objects
            doomed_ids=[],  # nothing deleted, so making ``first`` current leaves two heads
            links={first.id: None},
            versions={},
        )

    assert caught.value.__cause__ is None
    assert caught.value.context["constraint"] == "uq_memory_chain_head"
    assert _core_rows(seeded) == before


# --- the re-review of b29d24e9, L-A: a stale plan that leaves the head alone ------------


def _four_blocks(core: CoreMemoryStore, episodic: EpisodicStore, *cites: str) -> None:
    _evidence(episodic, "kept")
    for n, cited in enumerate(cites, start=1):
        _block(core, f"v{n}", cited)


def test_a_refresh_landing_before_a_middle_excise_does_not_leave_a_hole(
    seeded: Engine, embedder: HashEmbedder384
) -> None:
    """The forget read v1..v4 and dooms v2. A refresh appends v5 before the rewrite lands.

    The stale plan renumbers v3 and v4 as if v4 were still the newest, which does not touch
    the head, so the head index has nothing to object to and the chain was left with a hole
    that ``persona repair`` refuses for good.
    """
    racing = _RefreshLandsInsideTheForget(engine=seeded, embedder=embedder)
    core = CoreMemoryStore(backend=racing, audit_logger=MemoryAuditLogger())
    refresher, episodic, _ = _stores(seeded, embedder)
    _four_blocks(core, episodic, "kept", "gone", "kept", "kept")
    racing.hook = lambda: _block(refresher, "v5", "kept")

    assert core.forget_cited(_PERSONA, ["gone"]) == 1

    assert _core_rows(seeded) == [
        ("v1", 1, False),
        ("v3", 2, False),
        ("v4", 3, False),
        ("v5", 4, True),
    ]
    validate_chain(core.history(_PERSONA, _CHAIN))
    assert core.diagnose(_PERSONA).healthy


def test_two_forgets_planned_from_one_read_both_land_and_leave_a_valid_chain(
    seeded: Engine, embedder: HashEmbedder384
) -> None:
    racing = _RefreshLandsInsideTheForget(engine=seeded, embedder=embedder)
    slow = CoreMemoryStore(backend=racing, audit_logger=MemoryAuditLogger())
    fast, episodic, _ = _stores(seeded, embedder)
    _four_blocks(fast, episodic, "kept", "first-gone", "second-gone", "kept")
    racing.hook = lambda: fast.forget_cited(_PERSONA, ["first-gone"])

    assert slow.forget_cited(_PERSONA, ["second-gone"]) == 1

    assert _core_rows(seeded) == [("v1", 1, False), ("v4", 2, True)]
    validate_chain(slow.history(_PERSONA, _CHAIN))


def test_a_writer_arriving_inside_the_rewrite_waits_for_it(
    seeded: Engine, embedder: HashEmbedder384, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lock is what makes the check exact: between it and the commit, nobody moves a row.

    Probed from inside the transaction, after the check, by a second connection trying to update
    a row that survives the forget. It must be held off until the rewrite commits.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.exc import OperationalError

    backend = PostgresBackend(engine=seeded, embedder=embedder)
    core = CoreMemoryStore(backend=backend, audit_logger=MemoryAuditLogger())
    _, episodic, _ = _stores(seeded, embedder)
    _four_blocks(core, episodic, "kept", "gone", "kept", "kept")
    survivor = next(c.id for c in core.history(_PERSONA, _CHAIN) if c.text == "v4")
    outsider = create_engine(os.environ["DATABASE_URL"])
    held_off: list[bool] = []
    real = backend._relink_in  # noqa: SLF001 - the point inside the transaction to probe

    def probing(*args: Any, **kwargs: Any) -> None:  # noqa: ANN401 - forwards as given
        try:
            with outsider.connect() as conn:
                conn.execute(text("SET lock_timeout = '300ms'"))
                conn.execute(
                    text("UPDATE memory_chunks SET written_by = 'outsider' WHERE id = :id"),
                    {"id": survivor},
                )
                conn.commit()
            held_off.append(False)
        except OperationalError:
            held_off.append(True)
        real(*args, **kwargs)

    monkeypatch.setattr(backend, "_relink_in", probing)
    try:
        assert core.forget_cited(_PERSONA, ["gone"]) == 1
    finally:
        outsider.dispose()

    assert held_off == [True], "a writer moved a row between the check and the commit"
    validate_chain(core.history(_PERSONA, _CHAIN))
