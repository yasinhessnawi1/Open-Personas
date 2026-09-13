"""The pruning ceiling reaches the loop it configures (Spec W1, T10; D-W1-13).

D-W1-13's rollback is an env var: set
``PERSONA_API_AGENTIC_CONTEXT_COST_CEILING_TOKENS`` to 0 and an agentic run stops trimming
tool results. That promise is only real if the number travels all the way from the settings
object into the pruner the composed loop actually holds, and nothing in the runtime suite
can see that wiring: it starts one layer below, where the pruner is handed in.

So these build the loop through ``RuntimeFactory.build_agentic_loop`` and ask the loop's own
pruner what it would do. The DB-bound parts of the composition (the persona row, the
plan-selected tiers, the typed stores, the per-tenant MCP reads) are stubbed; the ceiling
path is exercised for real.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from persona.backends import BackendConfig
from persona.schema.conversation import ConversationMessage
from persona.schema.persona import Persona, PersonaIdentity
from persona_api.config import APIConfig
from persona_api.services.runtime_factory import RuntimeFactory
from persona_runtime.agentic.pruner import DEFAULT_COST_CEILING_TOKENS, ToolResultPruner
from persona_runtime.tier import TierConfig, TierRegistry

if TYPE_CHECKING:
    from persona_runtime.agentic.loop import AgenticLoop

_HUGE = " ".join(f"finding{n}" for n in range(20_000))  # far past any ceiling


def _persona() -> Persona:
    return Persona(
        persona_id="persona_ceiling_test",
        identity=PersonaIdentity(
            name="Astrid", role="assistant", background="A helper for ceiling wiring tests."
        ),
        tools=["web_search"],
    )


def _expensive_context() -> list[ConversationMessage]:
    return [
        ConversationMessage(role="system", content="floor", created_at=datetime.now(UTC)),
        ConversationMessage(
            role="tool",
            content=_HUGE,
            created_at=datetime.now(UTC),
            metadata={"tool_name": "web_search"},
        ),
    ]


async def _loop_from(config: APIConfig | None, monkeypatch: pytest.MonkeyPatch) -> AgenticLoop:
    registry = TierRegistry(
        {
            "frontier": TierConfig(
                name="frontier",
                backend_config=BackendConfig(provider="anthropic", model="m", api_key=None),  # type: ignore[arg-type]
            )
        }
    )
    factory = RuntimeFactory(
        rls_engine=None,  # type: ignore[arg-type]
        embedder=None,  # type: ignore[arg-type]
        tier_registry=registry,
        turn_log_writer=None,  # type: ignore[arg-type]
        audit_root=Path("/tmp/persona-ceiling-audit"),
        sandbox_pool=None,
        workspace_root=None,
        image_backend=None,
        api_config=config,
    )
    monkeypatch.setattr(RuntimeFactory, "_load_persona", lambda _self, _id: _persona())
    monkeypatch.setattr(RuntimeFactory, "_plan_tier_selection", lambda _self: (registry, None))
    monkeypatch.setattr(RuntimeFactory, "_build_stores", lambda _self: _fake_stores())

    async def _no_oauth(_self: object, _persona: object) -> None:
        """Config present means the MCP OAuth refresh runs, and that one needs a real DB."""

    monkeypatch.setattr(RuntimeFactory, "_refresh_oauth_before_inject", _no_oauth)
    monkeypatch.setattr(RuntimeFactory, "_build_byo_mcp_clients", lambda _self, _p: [])
    return await factory.build_agentic_loop("persona_ceiling_test")


def _fake_stores() -> dict[str, Any]:
    class _Store:
        def get_all(self, *_a: object, **_k: object) -> list[object]:
            return []

        def query(self, *_a: object, **_k: object) -> list[object]:
            return []

    return {kind: _Store() for kind in ("identity", "self_facts", "worldview", "episodic")}


def _pruner(loop: AgenticLoop) -> ToolResultPruner:
    return loop._pruner  # noqa: SLF001 (the wiring IS the assertion)


@pytest.mark.asyncio
async def test_a_zero_ceiling_in_the_config_reaches_the_loop_and_disables_pruning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rollback, end to end: the env var is the only thing anyone has to change."""
    config = APIConfig(agentic_context_cost_ceiling_tokens=0)
    loop = await _loop_from(config, monkeypatch)

    assert _pruner(loop).should_prune(_expensive_context()) is False


@pytest.mark.asyncio
async def test_a_configured_ceiling_is_the_one_the_loop_uses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not just on or off: the NUMBER travels. A context over the configured ceiling prunes,
    one under it does not, which a factory that ignored the config could not produce."""
    config = APIConfig(agentic_context_cost_ceiling_tokens=50)
    loop = await _loop_from(config, monkeypatch)
    small = [ConversationMessage(role="system", content="floor", created_at=datetime.now(UTC))]

    assert _pruner(loop).should_prune(_expensive_context()) is True
    assert _pruner(loop).should_prune(small) is False


@pytest.mark.asyncio
async def test_without_a_config_the_loop_gets_the_ruled_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A factory built without api settings (the CLI, tests) still guards: the default is
    the ruled 12,000, so the guard is on everywhere rather than only where settings happen
    to be wired."""
    loop = await _loop_from(None, monkeypatch)
    pruner = _pruner(loop)

    assert pruner.should_prune(_expensive_context()) is True
    assert pruner._ceiling == DEFAULT_COST_CEILING_TOKENS  # noqa: SLF001 (the number itself)
