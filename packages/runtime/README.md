# persona-runtime

> The conversation and agentic engine for Open Persona: the turn loop, the prompt
> builder, the router, and the plan, act, reflect cycle.

**License:** [MIT](LICENSE). Free for any use, including commercial.

`persona-runtime` is the orchestration layer of [Open Persona](../../README.md).
It turns a [`persona-core`](../core/README.md) persona into a running
conversational agent, and it depends only on `persona-core`. No HTTP, no database,
no secrets.

## What it is

The runtime owns the per turn lifecycle and the agentic loop, and nothing else.
Every collaborator (the persona registry, the model tiers, the toolbox, the
conversation object) is injected by the composition root: the API in production,
the CLI for local use, the tests in CI. The loop itself is stateless per request.

- **`ConversationLoop`**, the one turn keystone. Retrieve typed memory context,
  manage history (summarise and compact at K=10, keep the last 5 turns verbatim),
  build the prompt, route, stream generate with a tool call sub-loop, and write the
  turn back to the episodic store.
- **`PromptBuilder`** and `RetrievedContext`. Assembles the system prompt from
  identity, constraints, retrieved chunks, and the skill index, with a context
  window budget reducer. It also renders the **graph knowledge block**: an additive,
  independent source of what is known about the *user*, drawn from the shared
  knowledge graph, relevance gated and budgeted alongside the persona's own memory,
  with a versioned usage guidance artifact and a wellbeing care slot.
- **`retrieve_context`**, the per turn conditioning retrieval (identity via
  `get_all`, the rest via `query`). It was extracted so the voice trunk shares the
  *same* conditioning instead of reimplementing it. Optionally enriched with an
  owner scoped graph retrieval (`graph_selection.make_graph_retrieval`), queried
  independently of the persona stores.
- **Routing**, a deliberate surface to tier policy (`routing/policy.py`).
  `PolicyRouter` sits behind the `Router` Protocol and resolves each surface's
  stated tier: chat, authoring, and agentic get frontier, voice gets the latency
  tier, background gets small, recognition gets mid. A persona's pinned
  `tier_for_generation` still wins, because a deliberate override is honored.
  Layer-1 capability constraints such as vision still filter first. The earlier
  machinery is retained **dormant**: `HeuristicRouter` (the original rules),
  `UnifiedRouter` (constraint filter plus sweet spot scoring), and the
  `IntelligentRouter` model within tier scorer, the last of which is gated globally
  by `PERSONA_ROUTING_INTELLIGENT_ENABLED` (default off; repopulate model metadata
  before re-enabling).
- **`TierRegistry`**, a lazy cached backend registry per tier (`frontier` / `mid` /
  `small`), configured via `PERSONA_{TIER}_*` env triples, with small to mid to
  frontier fallback and cross provider multi model per tier.
- **`AgenticLoop`**, the plan, act, reflect cycle. One model decides at each step
  whether to call a tool, ask the user, or produce a final answer, with step history
  compaction at the tier budget, a cancel token boundary, and an authoritative
  terminal status (`completed` / `max_steps_reached` / `cancelled` / `error`).
- **`persona_runtime.legs`**, the leg executor for the autonomous task model. One
  leg is one bounded run of the **unmodified** `AgenticLoop`, book-ended by context
  reconstruction and a checkpoint write. It enforces the leg box (a wall clock trip
  at a step boundary, never mid step), writes the checkpoint through a sink port
  (the api's compare and set append), distils episodic memory at **milestone**
  granularity rather than spamming per leg, and uses the token bounded
  `CompactingCheckpointWriter` so a many leg task never overflows the checkpoint
  budget.
- **Safety, in the loop itself.** **Character adherence** is a never break rule with
  researched carve-outs: the persona never claims to be human when sincerely asked,
  and never roleplays through a wellbeing signal. The **turn time crisis gate** takes
  the persona out of the loop entirely on an acute, explicit signal, backed by a
  trained encoder for euphemistic and non English phrasing, with documented limits.
  Explicit acute is the reliability claim; subtle phrasing is a named residual, not a
  solved problem.
- **`TurnLog`** plus `JSONLTurnLogWriter` and `MemoryTurnLogWriter`, the per turn
  telemetry (model, tokens, cost, routing decision, latency, fallback), durable to
  JSONL or held in memory for tests.
- **`persona_runtime.extraction`**, the LLM half of the knowledge graph write paths:
  the grounded extraction pipeline (versioned prompt, one model call, grounded and
  restrained candidates), entity resolution with the AMBIGUOUS band judge, the
  `Synthesizer` (the off critical path reflection assembly), and the on by default
  `record_user_fact` direct write tool. It feeds the core graph's one merge.

## Install

```bash
pip install persona-runtime          # pulls in persona-core
```

Python 3.11 or newer. For workspace development from the monorepo:

```bash
git clone https://github.com/yasinhessnawi1/Open-Personas-ai.git
cd Open-Persona
uv sync --all-packages
```

## Quickstart

`persona-runtime` is a library with no CLI of its own. Compose it on top of
`persona-core`:

```python
import asyncio
from pathlib import Path

from persona.schema.persona import Persona
from persona.schema.conversation import Conversation, ConversationMessage
from persona.registry import PersonaRegistry
from persona.stores.chroma import ChromaMemoryStore
from persona.tools.toolbox import Toolbox
from persona_runtime import (
    ConversationLoop, PromptBuilder, Router, tier_registry_from_env,
)


async def main() -> None:
    persona = Persona.from_yaml(Path("examples/astrid_tenancy_law.yaml"))
    registry = PersonaRegistry(store=ChromaMemoryStore.local("./.persona-data"))
    registry.load(persona)
    tiers = tier_registry_from_env()

    loop = ConversationLoop(
        registry=registry,
        tiers=tiers,
        router=Router(),
        prompt_builder=PromptBuilder(),
        toolbox=Toolbox.empty(),
    )

    conversation = Conversation.new(persona_id=persona.id)
    user = ConversationMessage(role="user", content="Hva sier husleieloven om mugg?", created_at=None)
    async for chunk in loop.turn(conversation, user):
        print(chunk.delta, end="", flush=True)
    await tiers.aclose()


asyncio.run(main())
```

## Configuration

Each tier is configured by an env triple (see `.env.example` at the repo root):

```
PERSONA_FRONTIER_PROVIDER=anthropic   PERSONA_FRONTIER_MODEL=claude-opus-...
PERSONA_MID_PROVIDER=deepseek         PERSONA_MID_MODEL=deepseek-chat
PERSONA_SMALL_PROVIDER=groq           PERSONA_SMALL_MODEL=llama-...
```

A single `PERSONA_PROVIDER` plus `PERSONA_MODEL` plus `PERSONA_API_KEY` triple is
the fallback when no per tier vars are set.

## Architecture role

`persona-runtime` sits directly above [`persona-core`](../core/README.md) and below
`persona-api`. The API composes the runtime, attaches it to HTTP routes, and
persists the per request state (conversation, run, turn log, event bus). The
runtime contains zero HTTP, zero database clients, zero secrets. The voice trunk
([`persona-voice`](../voice/README.md)) reuses the runtime's reply producer, so a
voice turn is conditioned and routed exactly like a text turn.

## Test

```bash
uv run pytest packages/runtime                 # unit (default)
uv run pytest packages/runtime -m integration  # integration
uv run mypy packages/runtime/src
uv run ruff check packages/runtime
```

## License

`persona-runtime` is licensed under the **MIT License**, free for any use
including commercial. See [LICENSE](LICENSE). The application layer of Open Persona
(`persona-api`, `persona-web`) is separately licensed PolyForm Noncommercial
1.0.0; see the [root README](../../README.md) for the full per package table.

## Links

- [Open Persona root README](../../README.md)
- [`persona-core`](../core/README.md), the schema, memory stores, backends, tools
- [`persona-voice`](../voice/README.md), the real time voice trunk
- [CHANGELOG](CHANGELOG.md)
