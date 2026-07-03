"""Per-user concurrency caps — the shared denial-of-wallet primitive (Spec R7, R7-D-4).

ONE home for both forms of the hard per-user in-flight cap, consolidating the two
copy-pasted advisory-lock modules (``persona_api.imagegen.concurrency`` and
``persona_voice.concurrency``) that both now re-export from here (R7-D-4: "one
shared persona-core helper", mirroring the credits relocation). Two forms, because
"expensive op" has two shapes:

**Bounded ops** — the whole op runs inside ONE short transaction (image generation;
short synchronous metered calls). :func:`acquire_user_concurrency` generalizes the
shipped ``pg_try_advisory_xact_lock(hash(user_id))`` cap-1 primitive to a
configurable **N slots** via N distinct lock keys (``base_key XOR slot``, slot
0..N-1; try each, take the first free, fail fast if all held). Non-blocking, multi-
worker-correct (the lock lives in Postgres, not in-process), and **xact-scoped** —
auto-released on commit/rollback, so a mid-flight raise never leaks a slot.
``slots=1`` reproduces the old single-key behaviour **byte-for-byte** (``key XOR 0
== key``), so imagegen/voice keep cap-1 exactly.

**Long-running ops** — the work spans NO single short transaction (chat SSE, agentic
jobs), so the xact lock cannot hold across it. :func:`admit_long_op` /
:func:`release_long_op` use a **durable count** (the D-A0-6 ``jobs.state`` pattern): a
small ``inflight_ops`` registry row per admitted op (INSERT on start / DELETE on
completion), with admission gated by a count-under-cap check. This is the **HARD**
cap D-A0-6's anti-starvation gate is NOT — where money, not fairness, is at stake.

**Crash-safety (the durable form's load-bearing operational property).** A process
killed mid-op leaves its ``inflight_ops`` row behind; without reclamation that row
would eat a slot *forever* and the cap would ratchet shut. :func:`admit_long_op`
therefore sweeps rows older than ``ttl_seconds`` for the (user, op_class) BEFORE
counting — a leaked row is reclaimed once it ages past the TTL (which must exceed
the longest legitimate op, or a live op's slot would be reclaimed mid-run). This is
the same self-healing staleness-sweep discipline the integration conftest uses for
orphaned pooled connections; ``release_long_op`` is the clean-exit path that frees a
slot immediately.

Both forms **fail loud** at the API edge (over-cap ⇒ ``ConcurrencyCappedError`` →
429 + ``Retry-After``); the core helpers themselves stay error-free (yield/return a
status) so persona-core takes no persona-api dependency — the api/voice caller owns
the raise. ``slots``/``max_concurrent`` ``<= 0`` means **unlimited** (community /
uncapped edition no-op — every admission passes, nothing is written).
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from typing import TYPE_CHECKING

from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Connection, Engine

__all__ = [
    "DEFAULT_LONG_OP_TTL_SECONDS",
    "acquire_user_concurrency",
    "admit_long_op",
    "release_long_op",
]

# Default staleness horizon for the durable-count crash reclamation. Must exceed the
# longest legitimate long op (a slow agentic run) so a LIVE op's row is never swept
# mid-run — an hour is generous for a chat turn / agentic run and still reclaims a
# crashed process's slot within the hour. Callers may override per op_class.
DEFAULT_LONG_OP_TTL_SECONDS = 3600

# Sentinel returned by ``admit_long_op`` when the cap is unlimited (max <= 0). Never a
# real ``inflight_ops`` row id (those are uuid hex), so ``release_long_op`` no-ops it.
_UNLIMITED_TOKEN = "unlimited"

# The bounded-op N-slot advisory key: ``('x' || md5(:user_id))::bit(64)::bigint`` is
# the shipped stable-across-PG-versions derivation (see the old module docstrings);
# ``# :slot`` XORs the slot index in so slots 0..N-1 are N DISTINCT lock keys. slot=0
# leaves the key unchanged, so ``slots=1`` is byte-identical to the pre-R7 single-key
# ``pg_try_advisory_xact_lock(('x' || md5(:user_id))::bit(64)::bigint)``.
_TRY_SLOT_SQL = text(
    "SELECT pg_try_advisory_xact_lock((('x' || md5(:user_id))::bit(64)::bigint) # :slot) "
    "AS acquired"
)

# The durable-count admission critical section is serialized per (user, op_class) by a
# BLOCKING xact advisory lock so the count-then-insert can't interleave (two starts at
# count=max-1 both reading < max → over-admit). Namespaced with an ``inflight-admit:``
# prefix so its key space can't collide with the bounded-op slot keys above.
_ADMIT_LOCK_SQL = text(
    "SELECT pg_advisory_xact_lock(('x' || md5('inflight-admit:' || :key))::bit(64)::bigint)"
)
_SWEEP_STALE_SQL = text(
    "DELETE FROM inflight_ops WHERE user_id = :uid AND op_class = :cls "
    "AND started_at < now() - make_interval(secs => :ttl)"
)
_COUNT_INFLIGHT_SQL = text(
    "SELECT count(*) FROM inflight_ops WHERE user_id = :uid AND op_class = :cls"
)
_INSERT_INFLIGHT_SQL = text(
    "INSERT INTO inflight_ops (id, user_id, op_class) VALUES (:id, :uid, :cls)"
)
_DELETE_INFLIGHT_SQL = text("DELETE FROM inflight_ops WHERE id = :id AND user_id = :uid")


@contextmanager
def acquire_user_concurrency(
    *,
    conn: Connection,
    user_id: str,
    slots: int = 1,
) -> Iterator[bool]:
    """Try to hold one of ``slots`` per-user advisory transactional slots; yield status.

    The lock (if acquired) is held for the lifetime of the *enclosing* transaction on
    ``conn`` and auto-releases on commit or rollback — callers never release it
    themselves, so a mid-flight raise inside the surrounding ``rls_engine.begin()``
    frees the slot without leaking. Non-blocking: each slot is tried with
    ``pg_try_advisory_xact_lock`` (never waits), so the API responds with a fast 429
    rather than holding the HTTP connection while a prior op finishes.

    Args:
        conn: An OPEN SQLAlchemy connection already inside a transaction (from
            ``rls_engine.begin()``). Must not be autocommit — the xact lock needs an
            open transaction to scope its release.
        user_id: The opaque tenant id, hashed to the lock key (md5 → bigint;
            pessimistic-on-collision, see module docstring).
        slots: The per-user cap N. ``<= 0`` means unlimited (community/uncapped
            no-op) — yields ``True`` without taking any lock. ``1`` (default)
            reproduces the shipped cap-1 behaviour byte-for-byte.

    Yields:
        ``True`` if a slot is held (proceed with the protected op inside this same
        transaction), ``False`` if every slot is busy on other transactions (the
        caller raises ``ConcurrencyCappedError`` → 429 + ``Retry-After``).
    """
    if slots <= 0:
        yield True
        return
    for slot in range(slots):
        row = conn.execute(_TRY_SLOT_SQL, {"user_id": user_id, "slot": slot}).first()
        if row is not None and bool(row.acquired):
            yield True
            return
    yield False


def admit_long_op(
    *,
    rls_engine: Engine,
    user_id: str,
    op_class: str,
    max_concurrent: int,
    ttl_seconds: int = DEFAULT_LONG_OP_TTL_SECONDS,
) -> str | None:
    """Admit a long-running op under the per-user per-class HARD cap; return a token.

    Books a durable ``inflight_ops`` row iff the user currently holds fewer than
    ``max_concurrent`` of ``op_class``. The count-then-insert runs under a blocking
    per-(user, op_class) advisory lock so concurrent starts can't both pass a
    near-cap check (exactly ``max_concurrent`` are admitted). Before counting, rows
    older than ``ttl_seconds`` are swept so a crashed process's leaked row can't eat a
    slot forever (crash-safety — see module docstring).

    Returns:
        The op token to pass to :func:`release_long_op` on completion, or ``None`` if
        the cap is full (the caller raises ``ConcurrencyCappedError`` → 429). When
        ``max_concurrent <= 0`` (unlimited / community no-op) returns the sentinel
        :data:`_UNLIMITED_TOKEN` WITHOUT writing a row — always admitted.
    """
    if max_concurrent <= 0:
        return _UNLIMITED_TOKEN
    op_id = uuid.uuid4().hex
    with rls_engine.begin() as conn:
        # Serialize the admission decision for this (user, op_class).
        conn.execute(_ADMIT_LOCK_SQL, {"key": f"{user_id}:{op_class}"})
        # Crash-safety: reclaim slots leaked by killed processes before counting.
        conn.execute(_SWEEP_STALE_SQL, {"uid": user_id, "cls": op_class, "ttl": ttl_seconds})
        count = conn.execute(_COUNT_INFLIGHT_SQL, {"uid": user_id, "cls": op_class}).scalar_one()
        if int(count) >= max_concurrent:
            return None
        conn.execute(_INSERT_INFLIGHT_SQL, {"id": op_id, "uid": user_id, "cls": op_class})
    return op_id


def release_long_op(*, rls_engine: Engine, user_id: str, op_id: str | None) -> None:
    """Free a long-op slot by deleting its ``inflight_ops`` row (the clean-exit path).

    Idempotent and safe on every exit path (completion, error, client disconnect):
    a missing row (already swept as stale, or never written) is a no-op, and the
    :data:`_UNLIMITED_TOKEN` sentinel / ``None`` (community no-op / never-admitted)
    is ignored.
    """
    if not op_id or op_id == _UNLIMITED_TOKEN:
        return
    with rls_engine.begin() as conn:
        conn.execute(_DELETE_INFLIGHT_SQL, {"id": op_id, "uid": user_id})
