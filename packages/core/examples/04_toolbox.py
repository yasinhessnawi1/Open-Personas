"""Compose a persona's toolbox and dispatch tools directly — no model key required.

``build_default_toolbox`` assembles exactly what the persona YAML's ``tools``
allow-list grants (plus MCP servers if configured). Tools dispatch through one
audited door — the same one the runtime's tool-call sub-loop uses when a model
asks for a tool.

Run from ``packages/core/examples/``:

    uv run python 04_toolbox.py
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from persona.config import PersonaCoreConfig
from persona.errors import ToolNotAllowedError
from persona.schema.persona import Persona
from persona.schema.tools import ToolCall
from persona.tools import build_default_toolbox


async def main() -> None:
    persona = Persona.from_yaml(Path(__file__).parent / "viggo_code_reviewer.yaml")
    toolbox, mcp_clients = await build_default_toolbox(PersonaCoreConfig(), persona)
    try:
        print(f"{persona.identity.name}'s toolbox: {toolbox.names()}\n")

        # dispatch exactly as the runtime would when the model asks for a tool
        for call in (
            ToolCall(
                name="text_diff",
                args={
                    "a": "retries = 3\ntimeout = 10",
                    "b": "retries = 5\ntimeout = 10  # bumped for slow CI",
                },
            ),
            ToolCall(
                name="regex_match",
                args={"pattern": r"timeout = (\d+)", "text": "retries = 5\ntimeout = 10"},
            ),
        ):
            result = await toolbox.dispatch(call)
            body = result.content if isinstance(result.content, str) else repr(result.content)
            print(f"→ {call.name}({call.args})")
            print(f"  {body[:300].rstrip()}\n")

        # the allow-list is enforced at dispatch: the persona YAML grants tools,
        # and anything not granted is refused — even if the model asks for it
        try:
            await toolbox.dispatch(ToolCall(name="web_search", args={"query": "anything"}))
        except ToolNotAllowedError as exc:
            print(f"→ web_search (not in {persona.identity.name}'s YAML)")
            print(
                f"  refused: {exc.context.get('called')} not allowed; "
                f"allowed = {exc.context.get('allowed')}"
            )
    finally:
        for client in mcp_clients:
            await client.close()


asyncio.run(main())
