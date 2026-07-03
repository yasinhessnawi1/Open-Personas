"""F5 T16 — Spec 14 production-shape integration: upload → DB write → retrieval → prompt.

D-19-X-spec14-integration-test (chain entry 21).

F3 Phase 6 surfaced three integration gaps in the Spec 14 production
path that the unit + scripted-fake tests could NOT detect:

1. ``build_document_store`` wiring in ``app.py`` (real
   :class:`~persona.stores.postgres.PostgresBackend` over the RLS
   engine — alembic migration 005 adds the ``memory_chunks`` aux
   policy that lets the DocumentStore use ``persona_id =
   conversation_id`` legally).
2. The ``memory_chunks`` RLS aux policy on ``kind = 'document'``
   (migration ``005_memory_chunks_doc_rls.py``) — without it,
   document-chunk INSERTs raise ``InsufficientPrivilege`` because the
   typed-store policy assumes ``persona_id`` maps to ``personas.id``.
3. ``DocumentContext`` threading from
   ``chat_service.stream_chat`` → ``ConversationLoop.turn`` →
   ``PromptBuilder.build`` — the retrieved chunks must reach the
   rendered system prompt.

This test exercises the WHOLE production-shape path with NO fakes
between the upload bytes and the rendered prompt: real
``PostgresBackend``, real ``RuntimeFactory``, real workspace root,
real Alembic-migrated DB (incl. migration 005). The assertion is
end-to-end: the document chunk text appears in the prompt the
backend would receive.

Marked ``@pytest.mark.integration``. Skips cleanly when
``DATABASE_URL`` / ``APP_DATABASE_URL`` are not set — uses the same
RLS-fixture pattern as :mod:`test_runtime_factory` (the L7 locked
decision: reuse Spec 11 RLS conftest fixtures, do NOT touch source).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from persona.documents.ingest import IngestStrategy
from persona.schema.conversation import Conversation
from persona.stores.document_store import DocumentStore
from persona.stores.postgres import PostgresBackend
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from persona_api.services import document_service
from persona_api.services.runtime_factory import RuntimeFactory
from persona_api.storage import LocalFileStorage
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from persona.backends import StreamChunk as _StreamChunk
    from persona.backends import ToolSpec
    from persona.schema.conversation import ConversationMessage
    from sqlalchemy import Engine
    from tests.conftest import HashEmbedder384

pytestmark = pytest.mark.integration


_PERSONA_YAML = """\
schema_version: "1.0"
identity:
  name: Astrid
  role: Norwegian tenancy law assistant
  background: |
    Helps tenants understand husleieloven.
  language_default: en
  constraints: []
self_facts:
  - fact: knows tenancy law
    confidence: 1.0
"""


# A document body small enough to fit in the workspace but well above the
# default WHOLE_INJECT threshold (3000 tokens) so the ingest path lands
# on RETRIEVAL — which is the path that exercises the DocumentStore
# write + RLS aux policy. Crafting the body so each paragraph contains
# a distinctive token the test can grep for in the rendered prompt.
_DISTINCTIVE_TOKEN = "OPERA-HOUSE-BLUEPRINT-7421"
_DOCUMENT_BODY = (
    f"This contract references the {_DISTINCTIVE_TOKEN} clause. "
    "Tenant maintains heating systems quarterly under section 5-3 of "
    "husleieloven. Landlord retains structural integrity duty. "
    "Either party may terminate with two months written notice. "
    "Disputes go to Husleietvistutvalget for resolution. "
    "Common-area maintenance fees are billed quarterly. "
    "Subletting requires landlord written approval per section 7-1.\n\n"
) * 80  # ~4000+ tokens — over the 3000-token WHOLE_INJECT threshold.


class _ScriptedBackend:
    """Minimal ChatBackend — streams a fixed reply, no tools. Mirrors
    test_runtime_factory._ScriptedBackend; copied here (not imported) per
    the F5 LAND discipline (no test-tree refactor)."""

    provider_name = "anthropic"
    model_name = "scripted"
    max_tokens = 4096

    @property
    def supports_native_tools(self) -> bool:
        return True

    @property
    def supports_vision(self) -> bool:
        return False

    async def chat(self, messages: list[ConversationMessage], **_: object) -> object:  # noqa: ARG002
        raise NotImplementedError

    async def chat_stream(
        self,
        messages: list[ConversationMessage],
        *,
        tools: list[ToolSpec] | None = None,  # noqa: ARG002
        **_: object,
    ) -> AsyncIterator[_StreamChunk]:
        # Capture the prompt the backend would see so the test can assert
        # the document chunks reached it (the production-shape boundary).
        from persona.backends import StreamChunk, TokenUsage  # noqa: PLC0415

        self.captured_messages = list(messages)
        yield StreamChunk(delta="ack", is_final=False)
        yield StreamChunk(
            delta="",
            is_final=True,
            usage=TokenUsage(prompt_tokens=10, completion_tokens=1, total_tokens=11),
        )


class _ScriptedRegistry:
    """TierRegistry stub mirroring test_runtime_factory's shape."""

    def __init__(self) -> None:
        self._b = _ScriptedBackend()

    def get(self, _tier_name: str) -> _ScriptedBackend:
        return self._b

    @property
    def configured_tier_names(self) -> tuple[str, ...]:
        return ("frontier", "mid", "small")

    def supports_vision_for(self, _tier_name: str) -> bool:
        return self._b.supports_vision

    def metadata_for(self, _tier_name: str) -> None:
        return None

    def model_name_for(self, _tier_name: str) -> str:
        return "scripted"

    async def aclose(self) -> None:
        pass


class _NullTurnLog:
    def write(self, _log: object) -> None:
        pass


def _seed_persona_and_conversation(
    su_url: str, owner: str, persona_id: str, conversation_id: str
) -> None:
    """Seed users + personas + conversations rows under the superuser engine.

    The conversations row is required because the migration 005 aux RLS
    policy gates ``memory_chunks`` document writes on
    ``persona_id IN (SELECT id FROM conversations WHERE owner_id =
    current_user_id)``.
    """
    su = make_rls_engine(su_url)
    try:
        with su.begin() as conn:
            conn.execute(
                text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
                {"i": owner, "e": f"{owner}@x"},
            )
            conn.execute(
                text("INSERT INTO personas (id, owner_id, yaml) VALUES (:i, :o, :y)"),
                {"i": persona_id, "o": owner, "y": _PERSONA_YAML},
            )
            conn.execute(
                text(
                    "INSERT INTO conversations (id, owner_id, persona_id, title) "
                    "VALUES (:c, :o, :p, :t)"
                ),
                {"c": conversation_id, "o": owner, "p": persona_id, "t": "test"},
            )
    finally:
        su.dispose()


def _cleanup(su_url: str, owner: str) -> None:
    su = make_rls_engine(su_url)
    try:
        with su.begin() as conn:
            conn.execute(text("DELETE FROM users WHERE id = :i"), {"i": owner})
    finally:
        su.dispose()


@pytest.mark.asyncio
async def test_production_shape_upload_to_prompt(
    migrated_engine: Engine,  # noqa: ARG001 — fixture runs the migrations
    embedder: HashEmbedder384,
    tmp_path: Path,
) -> None:
    """End-to-end: upload via real document_service → DB write via real
    PostgresBackend (RLS-scoped) → retrieval inside the loop → prompt
    builder injects the doc chunk → assertion the chunk text reaches
    the backend's prompt.

    Skips cleanly when ``APP_DATABASE_URL`` is unset (the non-superuser
    RLS-scoped DSN). The ``DATABASE_URL`` superuser DSN is used to seed
    rows + drive the migration.
    """
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set; production-shape test requires RLS engine")
    su_url = os.environ["DATABASE_URL"]

    owner = "user_t16_docs_prod"
    persona_id = "persona_t16_docs_prod"
    conversation_id = "conv_t16_docs_prod"
    _seed_persona_and_conversation(su_url, owner, persona_id, conversation_id)

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()

    rls_engine = make_rls_engine(app_url)
    token = current_user_id.set(owner)
    try:
        # Gap #1 — build_document_store wiring: real PostgresBackend over
        # the RLS engine, just like app.py:174-176.
        document_backend = PostgresBackend(engine=rls_engine, embedder=embedder)
        document_store = DocumentStore(backend=document_backend)

        # Upload through the real document_service.upload — which
        # parses, chunks, and writes via DocumentStore.write. This is
        # the boundary that exercises gap #2 (migration 005 aux RLS
        # policy on memory_chunks for kind='document'). Without the
        # policy this write raises InsufficientPrivilege.
        ref = document_service.upload(
            file_storage=LocalFileStorage(workspace_root),
            owner_id=owner,
            persona_id=persona_id,
            conversation_id=conversation_id,
            file_bytes=_DOCUMENT_BODY.encode(),
            filename="contract.txt",
            document_store=document_store,
        )

        # Sanity: the document landed on the RETRIEVAL path (chunks were
        # written to the DocumentStore via the RLS-scoped engine).
        assert ref.strategy == IngestStrategy.RETRIEVAL, (
            f"document expected to land on RETRIEVAL path, got {ref.strategy}"
        )

        # The build_document_context boundary — gap #3 source. This is
        # what routes/conversations.py calls before stream_chat to build
        # the DocumentContext the runtime threads into PromptBuilder.
        document_context = document_service.build_document_context(
            file_storage=LocalFileStorage(workspace_root),
            owner_id=owner,
            persona_id=persona_id,
            conversation_id=conversation_id,
            user_message=f"What does the {_DISTINCTIVE_TOKEN} clause say?",
            document_store=document_store,
        )

        # Retrieval ran — chunks must have come back from the DB (proves
        # the write landed AND the RLS-scoped read sees them).
        assert len(document_context.retrieved_chunks) > 0, (
            "no chunks retrieved — RLS aux policy or DocumentStore write may be broken"
        )
        assert len(document_context.attached_documents) == 1, (
            "synopsis row missing — T16 structural defence regression"
        )

        # Real RuntimeFactory composing a real ConversationLoop — the
        # runtime composition root from app.py:223. The tier registry
        # is scripted (no real LLM call) so the test stays hermetic;
        # everything else is production-shape.
        factory = RuntimeFactory(
            rls_engine=rls_engine,
            embedder=embedder,
            tier_registry=_ScriptedRegistry(),  # type: ignore[arg-type]
            turn_log_writer=_NullTurnLog(),  # type: ignore[arg-type]
            audit_root=tmp_path / "audit",
        )
        loop = await factory.build_conversation_loop(persona_id)

        # Gap #3 — DocumentContext threading. Drive one turn through
        # ConversationLoop.turn with the document_context kwarg; the
        # backend's prompt is captured for the assertion.
        conv = Conversation(conversation_id=conversation_id, persona_id=persona_id, messages=[])
        async for _chunk in loop.turn(
            conv,
            f"Tell me about the {_DISTINCTIVE_TOKEN} clause.",
            document_context=document_context,
        ):
            pass

        # The PromptBuilder must have rendered the retrieved chunk into
        # the system prompt the backend received. This is the
        # end-to-end production-shape assertion.
        registry = factory._tier_registry  # noqa: SLF001 — test boundary
        scripted_backend = registry.get("frontier")  # type: ignore[attr-defined]
        captured = scripted_backend.captured_messages  # type: ignore[attr-defined]
        assert captured, "scripted backend never received a prompt"
        system_text = captured[0].content
        # The distinctive token from the document body must appear in
        # the system prompt — the retrieval path threaded the chunk all
        # the way through to PromptBuilder's rendered output.
        assert _DISTINCTIVE_TOKEN in system_text, (
            "document chunk text did NOT reach the prompt — "
            "F3 Phase 6 gap #3 (DocumentContext threading) is regressing"
        )
        # The synopsis row (T16) must also appear — Dominant Concern #2
        # structural defence.
        assert "Attached documents:" in system_text, (
            "synopsis row missing from prompt — T16 regression"
        )
    finally:
        current_user_id.reset(token)
        rls_engine.dispose()
        _cleanup(su_url, owner)
