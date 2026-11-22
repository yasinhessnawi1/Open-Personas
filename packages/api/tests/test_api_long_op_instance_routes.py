"""Each chat route hands the app's configured instance to the long-op admission (R9-318).

The boot sweep frees only the slots stamped with this instance's name, so a route that stamped
any other name would leave its slots for the hour-long TTL. Drives the three chat entry points
(post, regenerate, edit) over the community HTTP surface with a scripted loop and a
non-default ``instance_id``, and records what each admission was given.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import persona.concurrency as concurrency
from fastapi.testclient import TestClient
from persona.backends import StreamChunk, TokenUsage
from persona.schema.conversation import ConversationMessage
from persona_api.app import create_app
from persona_api.config import APIConfig, Edition

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    import pytest
    from persona.schema.conversation import Conversation
    from persona.stores.embedder import Embedder

_INSTANCE = "api-route-test"
_YAML = (
    "schema_version: '1.0'\n"
    "identity:\n"
    "  name: Sigrid\n"
    "  role: research assistant\n"
    "  background: A research assistant built to help with literature reviews.\n"
)


class _ScriptedLoop:
    """A conversation loop with no model behind it: one short reply per turn."""

    async def turn(
        self, conversation: Conversation, user_message: str, *_a: object, **_k: object
    ) -> AsyncIterator[StreamChunk]:
        now = datetime.now(UTC)
        conversation.messages.append(
            ConversationMessage(role="user", content=user_message, created_at=now)
        )
        yield StreamChunk(delta="Hello", is_final=False)
        conversation.messages.append(
            ConversationMessage(role="assistant", content="Hello", created_at=now)
        )
        yield StreamChunk(
            delta="",
            is_final=True,
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )


def test_post_regenerate_and_edit_stamp_the_configured_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, embedder: Embedder
) -> None:
    from persona_api.services import persona_service  # noqa: PLC0415

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)
    monkeypatch.setattr(persona_service, "default_embedder", lambda *_a, **_k: embedder)
    real_admit = concurrency.admit_long_op
    instances: list[object] = []

    def _recording_admit(**kwargs: object) -> str | None:
        instances.append(kwargs["instance_id"])
        return real_admit(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(concurrency, "admit_long_op", _recording_admit)
    config = APIConfig(
        edition=Edition.community,
        community_db_path=tmp_path / "community.db",
        community_memory_path=tmp_path / "chroma",
        workspace_root=tmp_path / "work",
        audit_root=str(tmp_path / "audit"),
        instance_id=_INSTANCE,
    )
    with TestClient(create_app(config)) as client:

        async def _build_loop(_persona_id: str) -> _ScriptedLoop:
            return _ScriptedLoop()

        client.app.state.build_conversation_loop = _build_loop  # type: ignore[attr-defined]
        pid = client.post("/v1/personas", json={"yaml": _YAML}).json()["id"]
        cid = client.post(f"/v1/personas/{pid}/conversations", json={"title": ""}).json()["id"]

        posted = client.post(f"/v1/conversations/{cid}/messages", json={"content": "hi there"})
        assert posted.status_code == 200, posted.text

        messages = client.get(f"/v1/conversations/{cid}").json()["messages"]
        assistant = next(m["id"] for m in reversed(messages) if m["role"] == "assistant")
        regenerated = client.post(f"/v1/conversations/{cid}/messages/{assistant}/regenerate")
        assert regenerated.status_code == 200, regenerated.text

        messages = client.get(f"/v1/conversations/{cid}").json()["messages"]
        user_message = next(m["id"] for m in reversed(messages) if m["role"] == "user")
        edited = client.patch(
            f"/v1/conversations/{cid}/messages/{user_message}/edit", json={"content": "hello"}
        )
        assert edited.status_code == 200, edited.text

    assert instances == [_INSTANCE, _INSTANCE, _INSTANCE]
