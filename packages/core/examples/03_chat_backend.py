"""One in-character turn against any of the ten providers — needs a model key.

The backend layer is one protocol over Anthropic, OpenAI, DeepSeek, Groq,
Together, NVIDIA, Cloudflare, OpenRouter, Ollama, and local HuggingFace.
Configure with env vars and the same code runs on any of them:

    export PERSONA_PROVIDER=anthropic          # or deepseek, groq, ollama, …
    export PERSONA_MODEL=claude-sonnet-4-6
    export PERSONA_API_KEY=sk-...

Run from ``packages/core/examples/``:

    uv run python 03_chat_backend.py
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

from persona.backends import BackendConfig, load_backend
from persona.backends.errors import ProviderError
from persona.schema.conversation import ConversationMessage
from persona.schema.persona import Persona


async def main() -> None:
    persona = Persona.from_yaml(Path(__file__).parent / "astrid_tenancy_law.yaml")
    try:
        backend = load_backend(BackendConfig())
    except ProviderError as exc:
        sys.exit(
            f"no backend configured ({exc}) — "
            "set PERSONA_PROVIDER / PERSONA_MODEL / PERSONA_API_KEY"
        )

    print(f"backend: {backend.provider_name} / {backend.model_name}\n")

    system = (
        f"You are {persona.identity.name}, {persona.identity.role}. "
        f"Constraints: {' '.join(persona.identity.constraints)}"
    )
    now = datetime.now(UTC)
    messages = [
        ConversationMessage(role="system", content=system, created_at=now),
        ConversationMessage(
            role="user",
            content="My landlord wants to raise my rent 15% mid-contract. One paragraph: can they?",
            created_at=now,
        ),
    ]

    # streaming — print tokens as they arrive
    print(f"{persona.identity.name}: ", end="", flush=True)
    async for chunk in backend.chat_stream(messages=messages):
        if chunk.delta:
            print(chunk.delta, end="", flush=True)
        if chunk.is_final:
            break
    print()


asyncio.run(main())
