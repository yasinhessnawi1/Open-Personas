"""What the persona has actually shown the user about their calendar.

Deleting a calendar entry is destructive and irreversible, and a model is perfectly capable
of producing a plausible schedule id it never read. So the write door
(``schedule_remove``) will not delete an id the user has not been shown: the id has to have
come out of a real calendar read, which is ``schedule_introspect`` listing the entry with
its subject, its cadence and its next run. That puts the entry, in words, in front of the
person before anything of theirs is removed.

This is the record of that. It is small on purpose:

* **Keyed by (owner, schedule id).** Nothing here is an existence oracle; a disclosure the
  ledger does not hold only ever means "ask first", never "that does not exist".
* **Time bounded.** A disclosure expires (:data:`DEFAULT_DISCLOSURE_TTL`), so "we talked
  about this last week" does not license a silent delete today.
* **Size bounded.** Oldest entries are dropped past :data:`DEFAULT_MAX_ENTRIES`, so a long
  running process cannot grow this without limit.

It lives in memory, held by the composition root and injected into both tools, so it is a
collaborator rather than global state. The honest limit of that: it does not survive a
restart, and with several API processes a later turn can land on one that never saw the
disclosure. Both cases degrade the same safe way, by asking the user again, and neither can
turn into a delete nobody asked for. A durable store would remove the second ask; it would
not make the gate any safer, which is why this stayed small.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

__all__ = [
    "DEFAULT_DISCLOSURE_TTL",
    "DEFAULT_MAX_ENTRIES",
    "ScheduleDisclosureLedger",
    "ScheduleDisclosures",
]

#: How long a disclosure licenses a delete. Long enough to span a conversation with pauses
#: in it, short enough that yesterday's calendar reading is not today's authorization.
DEFAULT_DISCLOSURE_TTL = timedelta(hours=6)

#: The cap on remembered disclosures. Oldest first out.
DEFAULT_MAX_ENTRIES = 4096


class ScheduleDisclosureLedger:
    """Schedule ids this system has shown their owner, with an expiry.

    Thread safe (the API serves turns on a thread pool and the tools are dispatched from
    several tasks), and deliberately offers no listing: the only question it answers is
    "has this owner been shown this id recently", which is the only question the delete
    gate asks.
    """

    def __init__(
        self,
        *,
        ttl: timedelta = DEFAULT_DISCLOSURE_TTL,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Build a ledger.

        Args:
            ttl: How long a disclosure stays valid.
            max_entries: The cap past which the oldest disclosures are dropped.
            clock: Returns "now" (tz-aware UTC). Injectable so expiry is testable.
        """
        self._ttl = ttl
        self._max_entries = max_entries
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)
        self._seen: dict[tuple[str, str], datetime] = {}
        self._lock = Lock()

    def record(self, owner_id: str, schedule_ids: Iterable[str]) -> None:
        """Note that ``owner_id`` has just been shown these schedule ids.

        A write, and it returns nothing (CQS). Blank owners and blank ids are ignored
        rather than stored, so an off-request read cannot seed the gate.
        """
        if not owner_id:
            return
        now = self._clock()
        with self._lock:
            for schedule_id in schedule_ids:
                if schedule_id:
                    self._seen[(owner_id, schedule_id)] = now
            self._evict_locked(now)

    def was_shown(self, owner_id: str, schedule_id: str) -> bool:
        """Whether this owner has been shown this id inside the TTL."""
        if not owner_id or not schedule_id:
            return False
        now = self._clock()
        with self._lock:
            shown_at = self._seen.get((owner_id, schedule_id))
            if shown_at is None:
                return False
            if now - shown_at > self._ttl:
                del self._seen[(owner_id, schedule_id)]
                return False
            return True

    def bind(self, owner_id: str) -> ScheduleDisclosures:
        """The owner-bound view the tools hold (resolved per dispatch, never per build)."""
        return _OwnerDisclosures(self, owner_id)

    def _evict_locked(self, now: datetime) -> None:
        """Drop expired entries, then the oldest, until the cap holds. Caller holds the lock."""
        expired = [key for key, at in self._seen.items() if now - at > self._ttl]
        for key in expired:
            del self._seen[key]
        if len(self._seen) <= self._max_entries:
            return
        oldest = sorted(self._seen.items(), key=lambda item: item[1])
        for key, _at in oldest[: len(self._seen) - self._max_entries]:
            del self._seen[key]


@runtime_checkable
class ScheduleDisclosures(Protocol):
    """One caller's half of the ledger, bound to their owner id by the composition root.

    The tools never handle an owner id themselves (that is a scope question, and scope
    questions are answered where the answers live), so they hold this instead. Resolved
    per dispatch, exactly like the schedule reader: a toolbox built once cannot carry one
    caller's disclosures into another's turn.
    """

    def record(self, schedule_ids: Iterable[str]) -> None:
        """Note that this caller has just been shown these schedule ids."""
        ...

    def was_shown(self, schedule_id: str) -> bool:
        """Whether this caller has been shown this id recently enough to act on it."""
        ...


class _OwnerDisclosures:
    """The concrete owner-bound view :meth:`ScheduleDisclosureLedger.bind` returns."""

    def __init__(self, ledger: ScheduleDisclosureLedger, owner_id: str) -> None:
        self._ledger = ledger
        self._owner_id = owner_id

    def record(self, schedule_ids: Iterable[str]) -> None:
        """Record these ids as shown to the bound owner."""
        self._ledger.record(self._owner_id, schedule_ids)

    def was_shown(self, schedule_id: str) -> bool:
        """Whether the bound owner has been shown this id inside the TTL."""
        return self._ledger.was_shown(self._owner_id, schedule_id)
