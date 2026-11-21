"""The initiative provisioner never schedules a persona whose create has not finished.

R9-293 round 2 LOW-5: a row inserted with ``create_complete`` false is either finishing (its
own create sets the flag) or abandoned (the erase sweep claims it). A scan schedule written
for it in between would outlive it or fire for a persona the owner never got.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from _erase_seed import row
from persona_api.db.community import create_community_schema, make_community_engine
from persona_api.initiative.provisioner import _CANDIDATES_SQL

if TYPE_CHECKING:
    from pathlib import Path

OWNER = "user_alice"


def test_only_finished_creates_are_candidates(tmp_path: Path) -> None:
    engine = make_community_engine(tmp_path / "community.db")
    try:
        create_community_schema(engine)
        with engine.begin() as conn:
            row(conn, "users", id=OWNER, email="alice@example.test")
            for pid, complete in (("persona_done", True), ("persona_half", False)):
                row(
                    conn,
                    "personas",
                    id=pid,
                    owner_id=OWNER,
                    yaml="x: 1",
                    create_complete=complete,
                )
        with engine.connect() as conn:
            candidates = [r[0] for r in conn.execute(_CANDIDATES_SQL, {"batch": 10})]
    finally:
        engine.dispose()
    assert candidates == ["persona_done"]
