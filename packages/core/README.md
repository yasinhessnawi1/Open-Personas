# persona-core

> One YAML file describes a persona. This library turns it into a running agent
> with typed memory, real tools, and a model tier picked per turn.

**License:** [MIT](LICENSE). Free for any use, including commercial.

`persona-core` is the foundation of [Open Persona](../../README.md): the
openly licensed engine every other package builds on, and the one package that
depends on nothing else in the project.

## What it is

A persona is a single typed YAML document: identity, constraints, self facts,
worldview claims with epistemic tags, tools, skills, routing preferences.
`persona-core` reads that document and gives you an agent you can drive from
Python or from the terminal. It ships:

- the **persona schema**, validator, and registry (frozen Pydantic v2 boundary
  models, `extra="forbid"`, deterministic chunk IDs);
- four **typed memory stores** (identity / self_facts / worldview / episodic)
  behind a `MemoryStore` protocol, with a file based **Chroma** backend (the
  default, zero infra) and a **Postgres + pgvector** backend (hosted);
- a **model backend** layer behind a `ChatBackend` protocol: Anthropic, OpenAI,
  DeepSeek, Groq, Together, NVIDIA, Cloudflare, OpenRouter (native tool calls),
  plus local **Ollama** and local **Hugging Face** (prompt shim fallback);
- a sandboxed **tool** layer (`Toolbox`, an MCP client, a known tool catalog, and
  built in tools) and a **skills** layer (`SkillScanner` + `SkillInjector` +
  composition + a `skills.toml` catalog + built in skill packs);
- an **image generation** layer, **vision** input, document ingestion and
  generation, a **code execution sandbox** protocol, an `AuditLogger` protocol
  with a JSONL default, per component loguru logging, and the **`persona` CLI**;
- a **durable job contract** (`persona.jobs`): the job model and state machine,
  frozen payloads, lease and retry policies, and a typed handler registry that the
  hosted worker composes (the queue and worker live in `persona-api`).
- a **schedule contract** (`persona.schedules`): the frozen schedule entity
  (RRULE class recurrence or a one time future, with the user's IANA timezone),
  the pure DST correct next fire computation (spring forward gap → adjusted
  instant; fall back fold → fire once), the missed fire policy decision, and the
  `schedule_id + fire_time` idempotency key and handoff contract (the durable
  store and the single leader tick live in `persona-api`).
- an **initiative contract** (`persona.initiative`): the restraint first policy
  core for a persona that notices and acts unprompted. The frozen, source tagged
  `InitiativeCandidate` (at least one grounding citation, a CLOSED trigger
  catalogue, a required concrete next step), the act within envelope versus
  propose at gates decision (fail closed: borderline proposes), the pure restraint
  policy (value threshold plus an acceptance SUPPRESSOR, per persona and per user
  cadence caps over trailing windows, quiet hours absolute delivery resolution,
  hold staleness), the per persona dial (`off / propose_only /
  act_within_envelope`, default propose only), and deterministic duplicate
  suppression arbitration (the scan, pipeline, and durable stores live in
  `persona-runtime` and `persona-api`).
- a **task contract** (`persona.tasks`): the durable entity *above* runs. The
  frozen `TaskCheckpoint` (conclusions, intent, pointers, size bounded, never
  transcripts) plus the `Task` state machine
  (`defined → active → waiting(…) → … → completed | failed | cancelled`), the cost
  ledger, the monotonic checkpoint sequence idempotency anchor, the A4 authored
  `Contract`, the pure context reconstruction ordering, the leg box, the resume
  trigger seam, and the outcome reports (the leg executor and the durable stores
  live in `persona-runtime` and `persona-api`).

## Install

```bash
pip install persona-core                 # core + Chroma + frontier provider SDKs
pip install persona-core[local]          # + torch / transformers for local HF inference
pip install persona-core[postgres]       # + psycopg + pgvector for the Postgres backend
pip install persona-core[sandbox]        # + docker SDK for the LocalDockerSandbox
pip install persona-core[turbovec]       # + turbovec for the optional quantized graph index
```

Python 3.11 or newer. For workspace development from the monorepo:

```bash
git clone https://github.com/yasinhessnawi1/Open-Personas-ai.git
cd Open-Personas-ai
uv sync --all-packages
```

## Quickstart

Author a persona and talk to it from the terminal. No API, no web app:

```bash
persona init                                      # interactive → a persona.yaml
persona validate examples/astrid_tenancy_law.yaml
export PERSONA_PROVIDER=groq                      # any of the ten providers
export PERSONA_MODEL=llama-3.3-70b-versatile
export PERSONA_API_KEY=<your-key>
persona chat examples/astrid_tenancy_law.yaml     # local REPL chat
persona audit examples/astrid_tenancy_law.yaml    # tail the JSONL audit log
```

## Examples

[`examples/`](examples/) ships five personas and four runnable scripts. Every
script except `03` runs **without a model key**, and each one is verified end to
end.

| Persona | What it shows |
| --- | --- |
| [`astrid_tenancy_law.yaml`](examples/astrid_tenancy_law.yaml) | A domain expert: Norwegian tenancy law, tools and skills wired, `nb` default language, honesty constraints. |
| [`kai_research.yaml`](examples/kai_research.yaml) | A research assistant. |
| [`maren_writing_coach.yaml`](examples/maren_writing_coach.yaml) | A tool free coach, where everything is voice and judgement. |
| [`viggo_code_reviewer.yaml`](examples/viggo_code_reviewer.yaml) | A senior code reviewer: sandboxed execution, diffs, severity ordered constraints. |
| [`embla_storyteller.yaml`](examples/embla_storyteller.yaml) | A folklore bedtime storyteller. Worldview rich, deliberately tool free. |

| Script | What it shows | Key needed |
| --- | --- | --- |
| [`01_load_and_inspect.py`](examples/01_load_and_inspect.py) | A persona is typed data: identity, constraints, confidence tagged self facts, epistemic tagged worldview. | no |
| [`02_typed_memory.py`](examples/02_typed_memory.py) | Write, semantic query, version, `history()`, `rollback()` on a real store, with the audit trail. | no |
| [`03_chat_backend.py`](examples/03_chat_backend.py) | One streamed in character turn. The same code runs on any of the ten providers via env vars. | yes |
| [`04_toolbox.py`](examples/04_toolbox.py) | Compose the persona's toolbox, dispatch real tools, and watch the allow list refuse an ungranted one. | no |

```bash
cd packages/core/examples
uv run python 02_typed_memory.py
```

```text
v1 written: 'Prefers espresso; drinks tea only when it rains.'
query hit:  'Prefers espresso; drinks tea only when it rains.'
v2 written: 'Switched to oat-milk cortados; espresso demoted to deadline fuel.'

history (2 versions):
  v1 (superseded)    'Prefers espresso; drinks tea only when it rains.'
  v2 (current)       'Switched to oat-milk cortados; espresso demoted to deadline fuel.'

after rollback, current: 'Prefers espresso; drinks tea only when it rains.'
```

## Usage

One streamed, in character turn. The provider is chosen entirely by env vars
(`PERSONA_PROVIDER` / `PERSONA_MODEL` / `PERSONA_API_KEY`):

```python
import asyncio
from datetime import UTC, datetime
from pathlib import Path

from persona.backends import BackendConfig, load_backend
from persona.schema.conversation import ConversationMessage
from persona.schema.persona import Persona


async def main() -> None:
    persona = Persona.from_yaml(Path("examples/astrid_tenancy_law.yaml"))
    backend = load_backend(BackendConfig())
    system = f"You are {persona.identity.name}, {persona.identity.role}."
    now = datetime.now(UTC)
    async for chunk in backend.chat_stream(messages=[
        ConversationMessage(role="system", content=system, created_at=now),
        ConversationMessage(role="user", content="Hva sier husleieloven om mugg?", created_at=now),
    ]):
        if chunk.delta:
            print(chunk.delta, end="", flush=True)
        if chunk.is_final:
            break


asyncio.run(main())
```

For the full conversation loop (router, tool dispatch, episodic write back, per
turn logging), compose `persona-core` with
[`persona-runtime`](../runtime/README.md).

## Capabilities

- **Typed memory, versioned.** Identity is immutable at runtime. self_facts,
  worldview, and episodic are append only with `history()` and `rollback()`. Every
  write is tagged with its source (`system` / `user` / `persona_self`) under a per
  store update policy, with a SHA-256 `content_hash` and exactly one `AuditEvent`
  per mutation.
- **Episodic memory is a multi resolution pyramid.** Raw chunks are kept forever
  (text plus embedding, because summaries never replace evidence), and a background
  engine builds gists above them with drill down pointers back to the untouched
  originals. Decay is usage reinforced (`R = exp(−Δt/(τ₀·strength))`, recall
  reinforces via `EpisodicStore.reinforce`), pinned or important memories never
  compress, and old memory is down rankable but never rank dead. Tunables live in
  `PERSONA_EPISODIC_*` (`persona.stores.lifecycle.EpisodicSettings`).
- **Ten model providers** behind one protocol, with native tool calls for
  Anthropic, OpenAI, DeepSeek, Groq, Together, NVIDIA, Cloudflare, and OpenRouter,
  plus a prompt shim fallback for local Ollama and HF. Embeddings via
  `bge-small-en-v1.5` (384 dim), recorded in the schema for re-index safety.
- **Tools.** Built ins include `web_search`, `web_fetch`, sandboxed `file_read` and
  `file_write` (the path resolver rejects `..`, absolute paths, symlink escape, NUL
  bytes, mixed separators), `calculator` (safe AST eval), `datetime`,
  `currency_convert`, `regex_match` (RE2, immune to ReDoS), `json_query` (JMESPath),
  `text_diff`, `text_summarize`, and `render_diagram`. A `TOOL_CATALOG` enumerates
  the full set for persona driven tool selection.
- **MCP.** A Streamable HTTP MCP client and adapter, plus built in MCP servers
  (`time` / `calculator` / `filesystem` / `weather`) as thin FastMCP subprocesses,
  indexed by a declarative `mcp_catalog.toml`.
- **Skills.** Four built in packs: `web_research`, `data_analysis`,
  `document_generation` (one parameterized skill spanning docx, pdf, pptx, xlsx, md,
  txt), and `code_review`. Injection is budgeted at 2k tokens
  (`SkillInjector.TOKEN_BUDGET`), composition goes three deep (cycle detection plus a
  shared budget), `collection:` refs resolve, and an alias shim keeps deprecated
  skill names working.
- **Skill injection trust.** Skills are prompt content the persona follows, so every
  skill, built in or an untrusted external `SKILL.md`, is injected through a
  **subordination guard** (`persona.skills.guard`): a nonce delimited, tier labelled
  envelope under a scope don't suppress authority preamble. Skill content can guide
  *how* the persona works but structurally cannot override its identity, the
  platform rules, the prompt's confidentiality, or its loyalties. Every skill carries
  a **trust tier** (`SkillTrust`: builtin / vetted / community / third_party, always
  **source assigned, never self declared**) and **provenance** (sha256
  `content_hash`). Activating a skill above `vetted` is **consent gated**
  (`SkillConsentPort`, default deny), and every injection, plus every consent
  refusal, emits an `AuditEvent`. This is defense in depth: structurally
  subordinated, tiered, consented, audited. It is **not** immunity (see
  `DEFENSE_CLAIM`).
- **Image generation** (OpenAI gpt-image-1, fal.ai Flux 1.1 [pro]) with a three
  layer safety filter plus a categorical hard line, and `craft_avatar_prompt`, a
  deterministic, demographically safe avatar prompt crafter.
- **Vision, documents, sandbox.** `ImageContent` vision input, document ingestion
  and generation, and a `CodeSandbox` protocol with a `LocalDockerSandbox` reference
  implementation.
- **Knowledge graph** (`persona.graph`). A user scoped bigger brain that all of a
  user's personas read from and write to: concept nodes connected by typed links
  (semantic, entity, temporal, causal), kept coherent by canonical entity resolution
  (deterministic, no LLM) plus accumulate via merge, with Postgres as the source of
  truth and an optional turbovec quantized in RAM dense index (pgvector is the
  default; 4-bit with mandatory exact rerank). RLS isolated per user, configured via
  `PERSONA_GRAPH_*` env vars. **Hybrid retrieval** (`HybridRetriever`) fuses the
  dense semantic leg and the sparse BM25/FTS leg via reciprocal rank fusion, in
  parallel and never gated, with bounded type aware traversal and an allowlist seam
  for user scope and wellbeing subtraction. This is the foundation of the K track:
  write paths, graph aware prompts, wellbeing, graph UI.
- **Write path contracts** (`persona.extraction`, `persona.wellbeing`). The frozen,
  LLM free shapes the graph's two feeders produce: the grounded
  `ExtractionCandidate` (a verbatim evidence span is required, so no quotable basis
  means no candidate), the `Extractor` and `EntityRecognizer` ports, and the shared
  `WellbeingCategory` vocabulary tagged at write time. The LLM extraction pipeline
  that fills them lives in the runtime.

## Architecture role

`persona-core` is the bottom layer of the Open Persona stack and the source
available foundation. A persona is a YAML document; the schema, the typed memory
stores, the model provider adapters, and the tool and skill machinery all live
here. [`persona-runtime`](../runtime/README.md) composes the orchestration loop on
top, `persona-api` exposes it over HTTP, and `persona-web` is the browser front
end. The dependency arrow points one way: this library imports nothing from the
upper layers.

It is also where the **community edition** does its persistence. The file based
Chroma backend holds typed memory locally with zero infrastructure. The Postgres
and pgvector backend is the same `MemoryStore` interface, swapped in for the cloud
edition.

## Test

```bash
uv run pytest packages/core                 # unit + contract (default)
uv run pytest packages/core -m integration  # needs Postgres in Docker
uv run mypy packages/core/src --strict
uv run ruff check packages/core
```

## License

`persona-core` is licensed under the **MIT License**, free for any use including
commercial. See [LICENSE](LICENSE). The application layer of Open Persona
(`persona-api`, `persona-web`) is separately licensed PolyForm Noncommercial
1.0.0; see the [root README](../../README.md) for the full per package table.

## Links

- [Open Persona root README](../../README.md)
- [`persona-runtime`](../runtime/README.md), the conversation and agentic engine
- [`persona-voice`](../voice/README.md), the real time voice trunk
- [CHANGELOG](CHANGELOG.md)
