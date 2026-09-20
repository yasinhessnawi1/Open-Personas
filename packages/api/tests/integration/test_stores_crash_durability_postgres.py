"""A versioned write survives a real kill on the PRODUCTION transport too (Spec K13, T2).

The Postgres half of ``packages/core/tests/integration/test_stores_crash_durability.py``, and
the half that matters most: the store that actually carries version chains in production is
``core_memory`` on Postgres, written by the background engine, and that worker is killed on
every deploy. Postgres opened its own transaction per backend call, so the old two call write
COMMITTED the supersede link and then died before the new head, leaving the link pointing at a
row that does not exist. One call is one transaction, so there is no longer an instant between
two writes to land on.

A real child process dies of ``SIGKILL`` mid write; the assertions run here, afterwards,
against rows read back out of the database.
"""

from __future__ import annotations

import signal
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

# ``packages/api/tests`` is not a package (no ``__init__.py``), so pytest puts this
# directory on ``sys.path`` and the sibling child module is a plain top level import.
import _crash_child_postgres as child
import pytest
from persona.audit import MemoryAuditLogger
from persona.stores.postgres import PostgresBackend
from persona.stores.self_facts import SelfFactsStore
from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy import Engine
    from tests.conftest import HashEmbedder384

pytestmark = pytest.mark.integration

_CHILD = Path(child.__file__)


@pytest.fixture
def seeded_engine(pg_engine: Engine) -> Engine:
    """``memory_chunks.persona_id`` is a foreign key; seed the owner and the persona."""
    with pg_engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email) VALUES ('u1', 'u1@example.com')"))
        conn.execute(
            text("INSERT INTO personas (id, owner_id, yaml) VALUES ('p1', 'u1', 'name: p1')")
        )
    return pg_engine


def _run_child_until_killed(database_url: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # noqa: S603 - fixed argv, this interpreter
        [sys.executable, str(_CHILD), database_url],
        capture_output=True,
        timeout=120,
        check=False,
    )


def test_a_versioned_write_survives_a_real_kill_on_postgres(
    seeded_engine: Engine, database_url: str, embedder: HashEmbedder384
) -> None:
    done = _run_child_until_killed(database_url)

    assert done.returncode == -signal.SIGKILL, (
        f"child did not die of SIGKILL (returncode={done.returncode}); "
        f"stderr={done.stderr.decode(errors='replace')[-2000:]}"
    )

    backend = PostgresBackend(engine=seeded_engine, embedder=embedder)
    store = SelfFactsStore(backend=backend, audit_logger=MemoryAuditLogger())

    rows = backend.get_all(persona_id=child.PERSONA_ID, store_kind="self_facts")
    assert rows, "the chain's first version did not survive; the test proves nothing"

    # The chain reads. Before the fix this raised BrokenVersionChainError, permanently.
    chain = store.history(child.PERSONA_ID, child.LOGICAL_ID)
    assert chain, "history() came back empty for a chain that has rows in the table"

    # Exactly one head, so an ordinary read can still find the node.
    heads = [c for c in rows if c.provenance is None or c.provenance.superseded_by is None]
    assert len(heads) == 1, (
        f"expected exactly one current head, got {len(heads)}: "
        f"{[(c.id, c.provenance.superseded_by if c.provenance else None) for c in rows]}"
    )
    assert [c.id for c in store.get_all(child.PERSONA_ID)] == [heads[0].id]
    assert heads[0].text in {child.TEXT_V1, child.TEXT_V2}

    # And the corruption stated as itself: no pointer to a row that is not there.
    present = {c.id for c in rows}
    dangling = [
        (c.id, c.provenance.superseded_by)
        for c in rows
        if c.provenance is not None
        and c.provenance.superseded_by is not None
        and c.provenance.superseded_by not in present
    ]
    assert not dangling, (
        f"a version points at a row that was never written: {dangling}. "
        "That is the state that makes history() and rollback() raise forever."
    )
