"""A versioned write survives the process being killed in the middle of it (Spec K13, T2).

`TypedStore.write` used to persist a versioned update as two backend calls with nothing around
them: the prior head carrying its new ``superseded_by`` link, and then the new head. A process
that died between the two left the link pointing at a chunk that was never written. The node
then had ZERO current heads, so an ordinary read could not find it, and the chain validator
made ``history()`` and ``rollback()`` raise on that logical id forever, with no repair anywhere
in the package. An outside crash experiment hit it on three of five kill seeds; measured here
before the fix, on this transport, with the randomised version of this test: four of eight.

This is the acceptance test for that, and it is deliberately not a mocked exception. A real
child process really dies of ``SIGKILL`` in the middle of the write, and the assertions run in
a different process against bytes read back off the disk. The test chooses only the instant:
the killer is armed for the update, so the kill lands where the torn state used to be made.

The kill window is closed by ONE upsert instead of two (D-K13-1), so with the fix in place
there is no instant between two writes to land on. Splitting that call back in two is the
mutation check, and it fails this file.
"""

from __future__ import annotations

import signal
import subprocess
import sys
from pathlib import Path  # noqa: TC003 - pytest tmp_path at runtime

import pytest
from persona.audit import MemoryAuditLogger
from persona.stores.chroma import ChromaBackend
from persona.stores.self_facts import SelfFactsStore

from tests._embedder import HashEmbedder
from tests.integration import _crash_child

pytestmark = pytest.mark.integration

_CHILD = Path(_crash_child.__file__)


def _run_child_until_killed(persist_path: Path) -> subprocess.CompletedProcess[bytes]:
    """Run the child to its own SIGKILL and return the finished process."""
    return subprocess.run(  # noqa: S603 - fixed argv, this interpreter
        [sys.executable, str(_CHILD), str(persist_path)],
        capture_output=True,
        timeout=120,
        check=False,
    )


def test_a_versioned_write_survives_a_real_kill(tmp_path: Path) -> None:
    """Kill the writer mid update; every logical id still answers ``history()`` and reads."""
    persist_path = tmp_path / "chroma"

    done = _run_child_until_killed(persist_path)

    # The child must have died BY THE KILL, not by raising, not by finishing. A negative
    # returncode is the signal that ended it; anything else means the test proved nothing.
    assert done.returncode == -signal.SIGKILL, (
        f"child did not die of SIGKILL (returncode={done.returncode}); "
        f"stderr={done.stderr.decode(errors='replace')[-2000:]}"
    )

    # A fresh process, a fresh client, whatever is on disk.
    backend = ChromaBackend(persist_path=persist_path, embedder=HashEmbedder())
    store = SelfFactsStore(backend=backend, audit_logger=MemoryAuditLogger())

    rows = backend.get_all(persona_id=_crash_child.PERSONA_ID, store_kind="self_facts")
    assert rows, "the chain's first version did not survive; the test proves nothing"

    # 1. The chain reads. Before the fix this raised BrokenVersionChainError, permanently.
    chain = store.history(_crash_child.PERSONA_ID, _crash_child.LOGICAL_ID)
    assert chain, "history() came back empty for a chain that has rows on disk"

    # 2. Exactly one head, so the node is reachable by an ordinary read. Before the fix there
    #    were zero: the prior head was marked superseded by a chunk that never landed.
    heads = [c for c in rows if c.provenance is None or c.provenance.superseded_by is None]
    assert len(heads) == 1, (
        f"expected exactly one current head, got {len(heads)}: "
        f"{[(c.id, c.provenance.superseded_by if c.provenance else None) for c in rows]}"
    )

    # 3. The ordinary read path finds it.
    current = store.get_all(_crash_child.PERSONA_ID)
    assert [c.id for c in current] == [heads[0].id]

    # 4. Whichever side of the kill the write fell on is fine; a torn chain is not. Both of
    #    these are honest outcomes of a killed process, and neither is the defect.
    assert heads[0].text in {_crash_child.TEXT_V1, _crash_child.TEXT_V2}


def test_the_chain_on_disk_has_no_dangling_link(tmp_path: Path) -> None:
    """The specific corruption, stated as itself: no pointer to a chunk that is not there."""
    persist_path = tmp_path / "chroma"

    done = _run_child_until_killed(persist_path)
    assert done.returncode == -signal.SIGKILL

    backend = ChromaBackend(persist_path=persist_path, embedder=HashEmbedder())
    rows = backend.get_all(persona_id=_crash_child.PERSONA_ID, store_kind="self_facts")
    present = {c.id for c in rows}

    dangling = [
        (c.id, c.provenance.superseded_by)
        for c in rows
        if c.provenance is not None
        and c.provenance.superseded_by is not None
        and c.provenance.superseded_by not in present
    ]
    assert not dangling, (
        f"a version points at a chunk that was never written: {dangling}. "
        "That is the state that makes history() and rollback() raise forever."
    )
