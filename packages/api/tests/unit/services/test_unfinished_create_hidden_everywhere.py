"""Every owner-facing read hides a persona whose create has not finished (R9-293 LOW-1).

The personas row is inserted with ``create_complete`` false and set true once the create
finished; until then it is either finishing or abandoned (the erase sweep claims it). Each
site below filters on the shared ``persona_complete`` clause, so the owner's own unfinished
row answers exactly as a persona that does not exist: a not-found (404) or a zero count.

Runs on the legacy community edition with both of the owner's personas set unfinished. Each
case names one site; dropping that site's clause fails its case (the mutation table in the PR).
Update and tool consent filter both their read and their write: either one alone answers
not-found here (the flag only ever goes from false to true), so it is dropping both that fails.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator  # noqa: TC003 - fixture and case annotations
from datetime import UTC, datetime
from pathlib import Path  # noqa: TC003 - a fixture annotation
from types import SimpleNamespace
from typing import Any

import pytest
from _erase_seed import OWNER, PERSONA, SIBLING, Seeded, legacy_edition, row
from fastapi import HTTPException
from persona.errors import OriginationForbiddenError, PersonaNotFoundError
from persona_api.config import Edition
from persona_api.db.community import create_community_schema, make_community_engine
from persona_api.db.models import personas as personas_t
from persona_api.errors import MCPServerNotFoundError
from persona_api.mcp import store as mcp_store
from persona_api.routes import autonomy
from persona_api.services import (
    chat_service,
    consent_service,
    nav_counts_service,
    persona_service,
    schedule_create_service,
    tool_consent_service,
    work_dispatch_service,
)
from persona_api.services.origination import OriginationRecorder
from persona_api.services.runtime_factory import RuntimeFactory
from sqlalchemy import update

_NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
_SERVER = "mcps_unfinished_create"
_YAML = """\
schema_version: "1.0"
identity:
  name: Astrid
  role: assistant
  background: A helper for the unfinished create test.
"""


@pytest.fixture
def hidden(tmp_path: Path, embedder: object) -> Iterator[Seeded]:
    """The legacy edition with both of the owner's personas left unfinished."""
    with legacy_edition(tmp_path, embedder) as legacy:  # type: ignore[arg-type]
        with legacy.engine.begin() as conn:
            conn.execute(
                update(personas_t)
                .where(personas_t.c.id.in_((PERSONA, SIBLING)))
                .values(create_complete=False, yaml=_YAML)
            )
            row(conn, "user_mcp_servers", id=_SERVER, owner_id=OWNER)
        yield legacy


def _update(s: Seeded, embedder: object) -> None:
    persona_service.update_persona(
        rls_engine=s.engine,
        embedder=embedder,  # type: ignore[arg-type]
        audit_root=s.audit_root,
        owner_id=OWNER,
        persona_id=PERSONA,
        yaml_str=_YAML,
        memory_backend=s.memory,
        audit_logger=s.audit,
    )


def _runtime_load(s: Seeded, _embedder: object) -> None:
    RuntimeFactory._load_persona(SimpleNamespace(_engine=s.engine), PERSONA)  # type: ignore[arg-type]  # noqa: SLF001


def _chat(s: Seeded, _embedder: object) -> None:
    chat_service.create_conversation(
        rls_engine=s.engine, owner_id=OWNER, persona_id=PERSONA, title="t"
    )


def _schedule_create(s: Seeded, _embedder: object) -> None:
    schedule_create_service._require_owned_persona(s.engine, owner_id=OWNER, persona_id=PERSONA)  # noqa: SLF001


def _work_dispatch(s: Seeded, _embedder: object) -> None:
    work_dispatch_service._require_persona(s.engine, PERSONA)  # noqa: SLF001


def _origination(s: Seeded, _embedder: object) -> None:
    recorder = OriginationRecorder(
        rls_engine=s.engine,
        episodic_store=None,  # type: ignore[arg-type]
        edition=Edition.community,
    )
    recorder._persist_message(  # noqa: SLF001
        SimpleNamespace(conversation_id=None),  # type: ignore[arg-type]
        owner_id=OWNER,
        persona_id=PERSONA,
    )


def _autonomy(s: Seeded, _embedder: object) -> None:
    try:
        autonomy._require_owned_persona(s.engine, OWNER, PERSONA)  # noqa: SLF001
    except HTTPException as exc:
        assert exc.status_code == 404  # noqa: PT017 - the status is the point
        raise PersonaNotFoundError("persona not found", context={"id": PERSONA}) from exc


def _mcp_store(s: Seeded, _embedder: object) -> None:
    mcp_store.assign_to_persona(rls_engine=s.engine, persona_id=PERSONA, server_id=_SERVER)


def _consent_read(s: Seeded, _embedder: object) -> None:
    consent_service.read_consent(rls_engine=s.engine, persona_id=PERSONA)


def _consent_set(s: Seeded, _embedder: object) -> None:
    consent_service.set_consent(rls_engine=s.engine, persona_id=PERSONA, granted=True, now=_NOW)


def _tool_consent(s: Seeded, embedder: object) -> None:
    tool_consent_service.grant_tool_consent(
        rls_engine=s.engine,
        embedder=embedder,  # type: ignore[arg-type]
        audit_root=s.audit_root,
        persona_id=PERSONA,
        owner_id=OWNER,
        tool_name="web_search",
        written_by=OWNER,
        now=_NOW,
        memory_backend=s.memory,
        audit_logger=s.audit,
    )


_NOT_FOUND: dict[str, tuple[Callable[[Seeded, object], None], type[Exception]]] = {
    "update": (_update, PersonaNotFoundError),
    "runtime load": (_runtime_load, PersonaNotFoundError),
    "chat": (_chat, PersonaNotFoundError),
    "schedule create": (_schedule_create, PersonaNotFoundError),
    "work dispatch": (_work_dispatch, PersonaNotFoundError),
    "origination": (_origination, OriginationForbiddenError),
    "autonomy": (_autonomy, PersonaNotFoundError),
    "mcp store": (_mcp_store, MCPServerNotFoundError),
    "consent read": (_consent_read, PersonaNotFoundError),
    "consent set": (_consent_set, PersonaNotFoundError),
    "tool consent": (_tool_consent, PersonaNotFoundError),
}


@pytest.mark.parametrize("site", sorted(_NOT_FOUND))
def test_an_unfinished_create_is_not_found(hidden: Seeded, embedder: object, site: str) -> None:
    call, refusal = _NOT_FOUND[site]
    with pytest.raises(refusal) as refused:
        call(hidden, embedder)
    context: dict[str, Any] = getattr(refused.value, "context", {}) or {}
    assert PERSONA in {str(v) for v in context.values()}, context


def test_nav_counts_count_no_unfinished_create(tmp_path: Path) -> None:
    """A fresh schema: the seed's task rows carry contracts the nav counts do not read."""
    engine = make_community_engine(tmp_path / "nav.db")
    try:
        create_community_schema(engine)
        with engine.begin() as conn:
            row(conn, "users", id=OWNER, email="owner@example.test")
            for pid, complete in ((PERSONA, False), (SIBLING, False)):
                row(conn, "personas", id=pid, owner_id=OWNER, yaml="x: 1", create_complete=complete)
        counts = nav_counts_service.get_nav_counts(engine, owner_id=OWNER, include_memory=False)
    finally:
        engine.dispose()
    assert counts["personas"] == 0
