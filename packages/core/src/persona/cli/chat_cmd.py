"""``persona chat <path>`` — REPL loop against a real backend.

Spec 02 wired this command to ``persona.backends.load_backend(BackendConfig())``.
The CLI streams the response so users see token-by-token output. Episodic
memory persists across REPL sessions because the registry indexes through a
stable ``PERSONA_CHROMA_PATH``.

Errors from the backend (missing API key, rate limit, etc.) surface as
domain exceptions and exit non-zero with a clear message — no silent
fallback to a fake backend (D-02-12).
"""
# ruff: noqa: B008 — typer.Argument/Option in defaults is the framework idiom

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path  # noqa: TC003 — typer needs runtime access
from typing import TYPE_CHECKING

import typer

from persona.audit import JSONLAuditLogger
from persona.backends import BackendConfig, load_backend
from persona.backends.errors import ProviderError
from persona.config import PersonaCoreConfig
from persona.history import ConversationHistoryManager
from persona.logging import get_logger
from persona.registry import PersonaRegistry
from persona.schema.chunks import ChunkProvenance, PersonaChunk, WriteSource, mint_chunk_id
from persona.schema.conversation import Conversation, ConversationMessage
from persona.stores import (
    ChromaBackend,
    EpisodicStore,
    IdentityStore,
    SelfFactsStore,
    SentenceTransformerEmbedder,
    WorldviewStore,
)

if TYPE_CHECKING:
    from persona.backends.protocol import ChatBackend
    from persona.stores.base import TypedStore

_log = get_logger("cli.chat")

__all__ = ["chat"]


def chat(
    persona_path: Path = typer.Argument(..., help="Path to a persona YAML."),
) -> None:
    """Start a REPL chat with the persona at ``persona_path``."""
    config = PersonaCoreConfig()
    try:
        backend = _build_backend()
    except ProviderError as exc:
        typer.echo(f"backend error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    embedder = SentenceTransformerEmbedder(model_name="BAAI/bge-small-en-v1.5")
    chroma = ChromaBackend(persist_path=config.chroma_path, embedder=embedder)
    audit_root = config.audit_path or (config.chroma_path / "audit")
    audit_logger = JSONLAuditLogger(audit_root)

    stores: dict[str, TypedStore] = {
        "identity": IdentityStore(backend=chroma, audit_logger=audit_logger),
        "self_facts": SelfFactsStore(backend=chroma, audit_logger=audit_logger),
        "worldview": WorldviewStore(backend=chroma, audit_logger=audit_logger),
        "episodic": EpisodicStore(backend=chroma, audit_logger=audit_logger),
    }
    registry = PersonaRegistry(stores=stores, audit_logger=audit_logger)
    persona = registry.load(persona_path)
    persona_id = persona.persona_id or persona_path.stem

    typer.echo(f"Loaded {persona.identity.name} ({persona_id}).")
    typer.echo(f"Backend: {backend.provider_name} / {backend.model_name}")
    typer.echo("Type a message and press Enter. Empty line to exit.\n")

    history_manager = ConversationHistoryManager()
    conversation = Conversation(
        conversation_id=f"cli-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}",
        persona_id=persona_id,
    )
    episodic_store = stores["episodic"]

    while True:
        try:
            user_input = typer.prompt("you", default="", show_default=False)
        except (KeyboardInterrupt, EOFError):
            typer.echo("\ngoodbye")
            return
        if not user_input.strip():
            typer.echo("goodbye")
            return

        user_msg = ConversationMessage(
            role="user", content=user_input, created_at=datetime.now(UTC)
        )
        conversation.messages.append(user_msg)

        prompt_messages = history_manager.manage(conversation, _no_op_summariser)
        try:
            reply_text = asyncio.run(_stream_reply(backend, prompt_messages, persona))
        except ProviderError as exc:
            typer.echo(f"backend error: {exc}", err=True)
            raise typer.Exit(code=1) from exc

        assistant_msg = ConversationMessage(
            role="assistant", content=reply_text, created_at=datetime.now(UTC)
        )
        conversation.messages.append(assistant_msg)

        # Episodic write-back unchanged from spec 01.
        _write_turn_to_episodic(
            episodic_store,
            persona_id=persona_id,
            user_text=user_input,
            assistant_text=reply_text,
        )


def _build_backend() -> ChatBackend:
    """Construct the configured backend; surfaces clear errors on misconfig."""
    config = BackendConfig()
    return load_backend(config)


async def _stream_reply(
    backend: ChatBackend,
    messages: list[ConversationMessage],
    persona: object,
) -> str:
    """Stream the assistant reply and print it token-by-token. Returns the full text."""
    typer.echo(f"{getattr(persona.identity, 'name', 'assistant')}: ", nl=False)  # type: ignore[attr-defined]
    parts: list[str] = []
    async for chunk in backend.chat_stream(messages=messages):
        if chunk.delta:
            typer.echo(chunk.delta, nl=False)
            parts.append(chunk.delta)
        if chunk.is_final:
            break
    typer.echo("")  # trailing newline
    return "".join(parts)


def _no_op_summariser(messages: list[ConversationMessage]) -> str:
    """Concatenate role+content; the small-tier model summariser lands in spec 05."""
    return " | ".join(f"{m.role}: {m.content[:60]}" for m in messages)


def _write_turn_to_episodic(
    store: TypedStore,
    *,
    persona_id: str,
    user_text: str,
    assistant_text: str,
) -> None:
    # Minted uuidv7 id (K8-D-6) — the former store-count index was an O(N)
    # read per write and raced under concurrent writers.
    chunk_id = mint_chunk_id(persona_id, "episodic")
    text = f"USER: {user_text}\nASSISTANT: {assistant_text}"
    now = datetime.now(UTC)
    store.write(
        persona_id,
        [
            PersonaChunk(
                id=chunk_id,
                text=text,
                metadata={"importance": "0.5"},
                created_at=now,
                provenance=ChunkProvenance(
                    source=WriteSource.SYSTEM,
                    logical_id=chunk_id,
                    version=1,
                    written_at=now,
                    written_by="cli.chat",
                ),
            ),
        ],
        source=WriteSource.SYSTEM,
        written_by="cli.chat",
    )
