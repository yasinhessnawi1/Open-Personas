"""Forgetting a memory forgets every version of it (Spec K13, T1; R9-201).

`delete_documents` matches on the PHYSICAL id, and the ids a forget is given come from a
search, which returns current versions only. So deleting exactly those ids removed the version
the user could see and left every superseded version of the same fact on disk, still holding
the words they asked to be rid of. It also left the older version pointing at a row that no
longer existed, which is the corruption T2 exists to prevent, produced deterministically with
no crash involved.

The assertions here are about CONTENT, not about row counts. A test that counts rows passes
happily while the forgotten sentence sits in version 1, so each one looks for the text itself
through a read that would find it if it were there, and the same read is shown finding it
before the delete so that the "gone" is worth something.

**On the episodic tests in this file.** Nothing in production writes a second version into the
episodic store: every episodic writer mints a fresh logical id, and a scan of production found
zero multi version episodic chains out of 333 rows. So the episodic cases below are a guard
against a future writer, NOT a reproduction of a live leak, and they are labelled that way
rather than left to imply we found data leaking (D-K11-11, R9-192). The stores that really
carry chains today are core_memory, self_facts and worldview, and the first test uses one of
those on purpose.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from persona.audit import AuditAction, MemoryAuditLogger
from persona.errors import BrokenVersionChainError
from persona.schema.chunks import ChunkProvenance, PersonaChunk, WriteSource
from persona.stores.self_facts import SelfFactsStore
from tests.unit.stores.test_versioned_store_integrity import _IdKeyedBackend

_NOW = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)
_PERSONA = "p1"
_KIND = "self_facts"
_SECRET = "my landlord is called Bjorn Haugen"


def _store(backend: _IdKeyedBackend) -> SelfFactsStore:
    return SelfFactsStore(backend=backend, audit_logger=MemoryAuditLogger())


def _chunk(text: str, *, chunk_id: str, logical_id: str | None = None) -> PersonaChunk:
    provenance = None
    if logical_id is not None:
        provenance = ChunkProvenance(
            source=WriteSource.USER, logical_id=logical_id, version=1, written_at=_NOW
        )
    return PersonaChunk(id=chunk_id, text=text, created_at=_NOW, provenance=provenance)


def _texts_on_disk(backend: _IdKeyedBackend) -> list[str]:
    """Every version's text, superseded ones included. The read a leak would show up in."""
    return [c.text for c in backend.get_all(persona_id=_PERSONA, store_kind=_KIND)]


def _edited_chain(backend: _IdKeyedBackend) -> tuple[SelfFactsStore, str]:
    """A fact told once and corrected twice: the secret survives only in version 1."""
    store = _store(backend)
    first = f"{_PERSONA}::{_KIND}::0000"
    store.write(_PERSONA, [_chunk(_SECRET, chunk_id=first)], source=WriteSource.USER)
    store.write(
        _PERSONA,
        [
            _chunk(
                "my landlord has a name I would rather not record", chunk_id=first, logical_id=first
            )
        ],
        source=WriteSource.USER,
    )
    store.write(
        _PERSONA,
        [_chunk("I would rather not talk about my landlord", chunk_id=first, logical_id=first)],
        source=WriteSource.USER,
    )
    return store, first


# --- the privacy claim, asserted on content ------------------------------------------------


def test_forgetting_the_current_version_forgets_the_words_in_the_older_ones() -> None:
    backend = _IdKeyedBackend()
    store, logical_id = _edited_chain(backend)

    # The read that would find a leak, finding it, so that "gone" below means something.
    assert any(_SECRET in text for text in _texts_on_disk(backend))

    head = store.get_all(_PERSONA)[0]
    store.remove_documents(_PERSONA, [head.id])

    assert not any(_SECRET in text for text in _texts_on_disk(backend)), (
        "the forgotten sentence is still on disk in a superseded version; deleting the head "
        "removed what the user could see and left what they asked to be rid of"
    )


def test_nothing_of_the_chain_survives_a_forget() -> None:
    backend = _IdKeyedBackend()
    store, logical_id = _edited_chain(backend)

    store.remove_documents(_PERSONA, [store.get_all(_PERSONA)[0].id])

    assert backend.get_all(persona_id=_PERSONA, store_kind=_KIND) == []
    assert store.history(_PERSONA, logical_id) == []


def test_a_forget_cannot_leave_a_chain_pointing_at_a_deleted_row() -> None:
    """R9-201's second consequence: the delete used to make T2's corruption on purpose."""
    backend = _IdKeyedBackend()
    store, _ = _edited_chain(backend)

    store.remove_documents(_PERSONA, [store.get_all(_PERSONA)[0].id])

    survivors = backend.get_all(persona_id=_PERSONA, store_kind=_KIND)
    present = {c.id for c in survivors}
    dangling = [
        c.id
        for c in survivors
        if c.provenance is not None
        and c.provenance.superseded_by is not None
        and c.provenance.superseded_by not in present
    ]
    assert not dangling


def test_forgetting_an_older_version_by_its_own_id_takes_the_whole_chain() -> None:
    """Whichever version the caller names, the fact goes. A half forgotten fact is worse."""
    backend = _IdKeyedBackend()
    store, _ = _edited_chain(backend)
    oldest = min(
        backend.get_all(persona_id=_PERSONA, store_kind=_KIND),
        key=lambda c: c.provenance.version if c.provenance else 0,
    )

    store.remove_documents(_PERSONA, [oldest.id])

    assert backend.get_all(persona_id=_PERSONA, store_kind=_KIND) == []


def test_the_audit_records_what_was_deleted_not_what_was_asked_for() -> None:
    """The trail is the proof afterwards, so it has to name all three rows, not one."""
    backend = _IdKeyedBackend()
    audit = MemoryAuditLogger()
    store = SelfFactsStore(backend=backend, audit_logger=audit)
    first = f"{_PERSONA}::{_KIND}::0000"
    store.write(_PERSONA, [_chunk(_SECRET, chunk_id=first)], source=WriteSource.USER)
    store.write(_PERSONA, [_chunk("v2", chunk_id=first, logical_id=first)], source=WriteSource.USER)
    head = store.get_all(_PERSONA)[0]

    store.remove_documents(_PERSONA, [head.id])

    removed = [e for e in audit.events if e.action is AuditAction.REMOVE_DOCUMENTS]
    assert len(removed) == 1
    assert len(removed[0].chunk_ids) == 2, (
        f"the audit says one row went when two did: {removed[0].chunk_ids}"
    )


# --- what must NOT widen --------------------------------------------------------------------


def test_a_chunk_with_no_provenance_deletes_exactly_itself() -> None:
    """A legacy row written before provenance belongs to no chain and must not drag others."""
    backend = _IdKeyedBackend()
    store = _store(backend)
    backend.upsert(
        persona_id=_PERSONA,
        store_kind=_KIND,
        chunks=[
            _chunk("legacy one", chunk_id="legacy-1"),
            _chunk("legacy two", chunk_id="legacy-2"),
        ],
    )

    store.remove_documents(_PERSONA, ["legacy-1"])

    assert [c.id for c in backend.get_all(persona_id=_PERSONA, store_kind=_KIND)] == ["legacy-2"]


def test_an_unknown_id_deletes_nothing_and_does_not_raise() -> None:
    backend = _IdKeyedBackend()
    store = _store(backend)
    store.write(_PERSONA, [_chunk("kept", chunk_id="kept")], source=WriteSource.USER)

    store.remove_documents(_PERSONA, ["never-existed"])

    assert [c.id for c in backend.get_all(persona_id=_PERSONA, store_kind=_KIND)] == ["kept"]


def test_two_unrelated_facts_do_not_take_each_other_with_them() -> None:
    backend = _IdKeyedBackend()
    store = _store(backend)
    store.write(_PERSONA, [_chunk("fact a", chunk_id="a")], source=WriteSource.USER)
    store.write(_PERSONA, [_chunk("fact b", chunk_id="b")], source=WriteSource.USER)

    store.remove_documents(_PERSONA, ["a"])

    assert [c.id for c in backend.get_all(persona_id=_PERSONA, store_kind=_KIND)] == ["b"]


def test_expanding_an_already_expanded_set_adds_nothing() -> None:
    """``chain_ids`` is called twice on the episodic path; it has to be idempotent."""
    backend = _IdKeyedBackend()
    store, _ = _edited_chain(backend)
    head = store.get_all(_PERSONA)[0]

    once = store.chain_ids(_PERSONA, [head.id])
    twice = store.chain_ids(_PERSONA, once)

    assert once == twice
    assert len(once) == 3


# --- forget beats rollback (Spec K13, acceptance 3) ----------------------------------------


def test_a_forgotten_fact_cannot_be_rolled_back_into_existence() -> None:
    """The two features that could undo each other, pointed at each other on purpose.

    Rollback exists to bring an older version back, and forget exists to make sure no version
    comes back at all. If a forget left any part of the chain behind, rollback is the tool
    that would find it and make it current again, which is the worst possible way for a
    privacy promise to fail: not a leak somebody has to dig for, a leak the product offers.

    The spec made this test conditional on the K11 interaction being live. It is not live
    (nothing writes episodic chains), so this is a guard rather than a reproduction, and it
    is worth having anyway because the two features are permanent and the interaction is not
    obvious from either side.
    """
    backend = _IdKeyedBackend()
    store, logical_id = _edited_chain(backend)
    head = store.get_all(_PERSONA)[0]

    store.remove_documents(_PERSONA, [head.id])

    with pytest.raises(BrokenVersionChainError, match="no chain"):
        store.rollback(_PERSONA, logical_id, to_version=1, source=WriteSource.USER)
    assert not any(_SECRET in text for text in _texts_on_disk(backend))
