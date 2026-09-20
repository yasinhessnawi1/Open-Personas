"""Every key that becomes chunk metadata is classified, and a rollback carries only payload.

Spec K13, T4 (D-K13-12). Two halves, and the second one is the task.

**A rollback must not carry bookkeeping back in time.** ``rollback`` copies an older version's
text forward as a new current version. Some metadata keys describe that text and belong with
it; some are how another subsystem finds or gates the row, and carrying those forward stamps
the new head with a conversation id a delete keys on (pointing at a conversation that may be
gone) or a session id a cooldown gate reads (reviving a session that ended).

**And the classification has to be enforced, not documented.** A denylist a future stamp site
can walk past silently is the §6b failure this project keeps repeating: the register would
still be exactly right about the two keys we knew about in September and silently wrong about
the one added in November. So the guard below walks every place in ``packages/*/src`` that
puts a key into chunk metadata and fails on any key that is in neither set. Adding a stamp
site without classifying its key breaks the build.

The walk deliberately covers more than ``PersonaChunk(metadata={...})``, because four of the
real stamp sites do not look like that: ``registry`` passes its dicts to a ``_chunk_...``
helper, and ``autonomy``, the runtime loop and the document builder assemble a local bag and
hand it over by name. A guard that only saw the obvious shape would have passed while missing
the keys that matter most.
"""

from __future__ import annotations

import ast
import importlib
from dataclasses import dataclass
from pathlib import Path

import pytest
from persona.audit import MemoryAuditLogger
from persona.schema.chunks import (
    BOOKKEEPING_METADATA_KEYS,
    PAYLOAD_METADATA_KEYS,
    ChunkProvenance,
    PersonaChunk,
    WriteSource,
)
from persona.stores.self_facts import SelfFactsStore
from tests.unit.stores.test_versioned_store_integrity import _IdKeyedBackend

_PACKAGES = Path(__file__).resolve().parents[4]

#: Names a local metadata bag is built under before it is handed to a chunk.
_BAG_NAMES = frozenset({"metadata", "meta", "md"})
_CHUNK_TYPES = frozenset({"PersonaChunk", "DocumentChunk"})

#: ``metadata=`` arguments that are NOT a dict literal, each acknowledged with the reason it
#: introduces no keys of its own. A NEW one fails the guard until somebody classifies it,
#: which is the difference between a blind spot and a decision.
_ACKNOWLEDGED_INDIRECT: dict[str, str] = {
    "persona/autonomy.py": "local bag; its keys are read from the assignments above it",
    "persona/registry.py": "local bag; its keys are read from the literals passed in",
    "persona/schema/documents.py": "local bag; its keys are read from the literals above it",
    "persona/stores/base.py": "rollback copies an existing chunk's metadata; adds nothing",
    "persona/stores/chroma.py": "read path: rebuilds metadata that was already stored",
    "persona/stores/postgres.py": "read path: rebuilds metadata that was already stored",
    "persona_runtime/loop.py": "local bag; its keys are read from the assignments above it",
}


@dataclass(frozen=True)
class _Site:
    key: str
    path: str
    line: int


def _callee(call: ast.Call) -> str:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return ""


def _module_name(path: Path) -> str:
    parts = path.relative_to(_PACKAGES).parts  # e.g. core/src/persona/autonomy.py
    return ".".join(parts[2:]).removesuffix(".py")


def _resolve(key_node: ast.expr, path: Path) -> str:
    """A literal key, or a constant's VALUE looked up in the module that uses it.

    Four stamp sites name their key through a module constant, and a guard that compared
    constant NAMES would be checking the wrong string. Importing the module and reading the
    attribute is what makes ``ORIGINATED_METADATA_KEY`` and ``"originated"`` the same key.
    """
    if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str):
        return key_node.value
    if isinstance(key_node, ast.Name):
        module = importlib.import_module(_module_name(path))
        resolved = getattr(module, key_node.id, None)
        if isinstance(resolved, str):
            return resolved
        return f"<unresolved:{key_node.id}>"
    return "<computed>"


def _collect() -> tuple[list[_Site], list[tuple[str, int]]]:
    """Every metadata key written anywhere in src, and every indirect handover."""
    sites: list[_Site] = []
    indirect: list[tuple[str, int]] = []

    for path in sorted(_PACKAGES.glob("*/src/**/*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = str(path.relative_to(_PACKAGES)).split("src/", 1)[-1]
        functions = [
            n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
        ]
        for fn in functions:
            calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
            chunky = [
                c for c in calls if _callee(c) in _CHUNK_TYPES or "chunk" in _callee(c).lower()
            ]
            for call in chunky:
                for kw in call.keywords:
                    if kw.arg != "metadata":
                        continue
                    if isinstance(kw.value, ast.Dict):
                        sites.extend(
                            _Site(_resolve(k, path), rel, call.lineno)
                            for k in kw.value.keys
                            if k is not None
                        )
                    elif _callee(call) in _CHUNK_TYPES:
                        indirect.append((rel, call.lineno))

            if not any(_callee(c) in _CHUNK_TYPES for c in calls):
                continue
            # A bag assembled locally, then handed over by name.
            for node in ast.walk(fn):
                if not isinstance(node, ast.Assign):
                    continue
                for target in node.targets:
                    if (
                        isinstance(target, ast.Name)
                        and target.id in _BAG_NAMES
                        and isinstance(node.value, ast.Dict)
                    ):
                        sites.extend(
                            _Site(_resolve(k, path), rel, node.lineno)
                            for k in node.value.keys
                            if k is not None
                        )
                    if (
                        isinstance(target, ast.Subscript)
                        and isinstance(target.value, ast.Name)
                        and target.value.id in _BAG_NAMES
                    ):
                        sites.append(_Site(_resolve(target.slice, path), rel, node.lineno))
    return sites, indirect


def test_every_metadata_key_in_the_tree_is_classified() -> None:
    """The guard. A new stamp site must say which kind of key it is adding."""
    sites, _ = _collect()
    known = BOOKKEEPING_METADATA_KEYS | PAYLOAD_METADATA_KEYS

    unclassified = sorted({(s.key, s.path, s.line) for s in sites if s.key not in known})

    assert not unclassified, (
        "these metadata keys are in neither BOOKKEEPING_METADATA_KEYS nor "
        f"PAYLOAD_METADATA_KEYS in persona.schema.chunks: {unclassified}. Decide which it is: "
        "a key is BOOKKEEPING when another subsystem uses it to find, delete or gate the "
        "chunk (a rollback must not carry it), and payload otherwise."
    )


def test_the_guard_actually_found_the_stamp_sites() -> None:
    """A walk that finds nothing passes every check ever written against it."""
    sites, _ = _collect()
    found = {s.key for s in sites}

    assert len(sites) > 20, f"the walk found only {len(sites)} keys; it has stopped working"
    # One from each shape the walk has to cope with, so a regression in any one shows up.
    assert "conversation_id" in found, "missed a literal dict on a PersonaChunk call"
    assert "epistemic" in found, "missed a dict passed to a _chunk_... helper (registry)"
    assert "autonomy_level" in found, "missed a locally assembled bag (autonomy)"
    assert "session_id" in found, "missed a subscript write onto a bag (autonomy)"
    assert "originated" in found, "missed a key named by a module constant"
    assert "<computed>" not in found, "a key is computed at runtime and cannot be classified"
    assert not any(k.startswith("<unresolved") for k in found), f"unresolved constants: {found}"


def test_indirect_handovers_are_acknowledged_one_by_one() -> None:
    """Silence is the failure mode: an unlisted indirect site must not pass unnoticed."""
    _, indirect = _collect()

    unacknowledged = sorted(
        {(path, line) for path, line in indirect if path not in _ACKNOWLEDGED_INDIRECT}
    )

    assert not unacknowledged, (
        f"these chunk constructions pass metadata that is not a literal: {unacknowledged}. "
        "Either the keys are assembled somewhere this guard can read, or the site needs a "
        "line in _ACKNOWLEDGED_INDIRECT saying why it introduces no keys of its own."
    )


def test_the_two_sets_do_not_overlap() -> None:
    assert not (BOOKKEEPING_METADATA_KEYS & PAYLOAD_METADATA_KEYS)


# --- the rollback behaviour, one test per bookkeeping key we actually use ------------------

_NOW_SOURCE = WriteSource.USER


def _seed(store: SelfFactsStore, metadata: dict[str, str]) -> str:
    from datetime import UTC, datetime

    now = datetime(2026, 9, 20, tzinfo=UTC)
    chunk_id = "p1::chain::0001"
    store.write(
        "p1",
        [
            PersonaChunk(
                id=chunk_id,
                text="the original wording",
                metadata=metadata,
                created_at=now,
                provenance=ChunkProvenance(
                    source=_NOW_SOURCE, logical_id=chunk_id, version=1, written_at=now
                ),
            )
        ],
        source=_NOW_SOURCE,
    )
    store.write(
        "p1",
        [
            PersonaChunk(
                id=chunk_id,
                text="the newer wording",
                metadata=metadata,
                created_at=now,
                provenance=ChunkProvenance(
                    source=_NOW_SOURCE, logical_id=chunk_id, version=1, written_at=now
                ),
            )
        ],
        source=_NOW_SOURCE,
    )
    return chunk_id


@pytest.mark.parametrize("bookkeeping_key", sorted(BOOKKEEPING_METADATA_KEYS))
def test_a_rollback_leaves_every_bookkeeping_key_behind(bookkeeping_key: str) -> None:
    """Parametrised over the register itself, so a key added later is covered on arrival."""
    backend = _IdKeyedBackend()
    store = SelfFactsStore(backend=backend, audit_logger=MemoryAuditLogger())
    logical_id = _seed(store, {bookkeeping_key: "stamped-long-ago", "importance": "0.5"})

    store.rollback("p1", logical_id, to_version=1, source=_NOW_SOURCE)

    head = store.get_all("p1")[0]
    assert head.text == "the original wording", "the rollback did not bring the text back"
    assert bookkeeping_key not in head.metadata, (
        f"the rollback carried {bookkeeping_key!r} forward onto a new current version; "
        "another subsystem reads that as current and has no way to know it time travelled"
    )


def test_a_rollback_keeps_the_metadata_that_describes_the_text() -> None:
    """The other half: stripping everything would be just as wrong, and quieter."""
    backend = _IdKeyedBackend()
    store = SelfFactsStore(backend=backend, audit_logger=MemoryAuditLogger())
    logical_id = _seed(
        store, {"conversation_id": "c-123", "importance": "0.9", "confidence": "0.800"}
    )

    store.rollback("p1", logical_id, to_version=1, source=_NOW_SOURCE)

    head = store.get_all("p1")[0]
    assert head.metadata == {"importance": "0.9", "confidence": "0.800"}


def test_the_older_versions_keep_their_own_stamps() -> None:
    """Stripping is about the NEW head. History is a record and must not be rewritten."""
    backend = _IdKeyedBackend()
    store = SelfFactsStore(backend=backend, audit_logger=MemoryAuditLogger())
    logical_id = _seed(store, {"conversation_id": "c-123", "importance": "0.5"})

    store.rollback("p1", logical_id, to_version=1, source=_NOW_SOURCE)

    chain = store.history("p1", logical_id)
    assert chain[0].metadata["conversation_id"] == "c-123"
    assert "conversation_id" not in chain[-1].metadata
