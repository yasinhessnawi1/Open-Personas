"""LLM-assisted persona authoring (spec 10, T03, §3.3 / §4 / D-10-2, D-10-3).

Turns a natural-language description into a **draft** persona envelope
(:class:`AuthoringDraft`) — YAML + clarifying questions + the prompt version —
WITHOUT creating a persona row. The user reviews/refines the draft, then saves
via ``POST /v1/personas`` (which creates the row).

The retry-with-error-feedback loop here is the *model-agnosticism mechanism*
(D-10-3): the prompt raises compliance probability, but this loop is what makes
the contract hold regardless of model — it feeds Pydantic's validation errors
back to the model and re-asks once. The backend is injected (the route resolves
it from the TierRegistry), so this service is decoupled from provider wiring and
testable with a scripted backend.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

import yaml
from persona.billing import CostBasis  # noqa: TC001 — Pydantic needs the field type at runtime
from persona.schema.conversation import ConversationMessage
from persona.schema.persona import Persona
from persona.tools import TOOL_CATALOG, known_tool_names
from persona.tools.mcp.catalog import (
    BUILTIN_MCP_CATALOG,
    known_mcp_server_names,
    mcp_server_entry,
    recommender_provider_tag,
)
from persona_runtime.cost import compute_turn_cost
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from persona_api.schemas.responses import AuthoringDraft, ToolRecommendation
from persona_api.services.authoring_parse import split_response
from persona_api.services.authoring_prompt import (
    AUTHORING_PROMPT_VERSION,
    QUESTIONS_MARKER,
    build_authoring_prompt,
    build_refinement_prompt,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from persona.backends import ChatBackend
    from persona.backends.types import TokenUsage
    from persona_runtime.cost import CostSource


class AuthoringCost(BaseModel):
    """The real metered cost of one authoring generation (Spec M3, T2a).

    Surfaced by the streamed authoring generators just before the terminal draft,
    mirroring the chat loop's ``last_turn_cost_cents`` / ``last_turn_cost_basis``
    (``compute_turn_cost`` over the served backend's provider/model/tokens, with an
    OpenRouter ``usage.cost`` actual taking precedence). ``cost_cents`` is the SUM
    across every generation the turn made (the validation-repair retry is a second
    paid call, D-10-3), so the route bills the whole authoring turn once. The route
    prices credits from this via the M3 formula (floored — infra rides the floor,
    D-M3-4 amendment) and records ``cost_cents`` / ``cost_basis`` on the ledger row.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    cost_cents: float = Field(ge=0.0)
    cost_basis: CostBasis
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)


#: One semantic event yielded by the streamed authoring generators (spec P0,
#: D-P0-service-yields-events). The service is transport-agnostic — it knows
#: nothing about SSE; the route maps each event to a ``text/event-stream`` frame.
#: ``("chunk", delta)`` — a forming-text fragment;
#: ``("retry", reason)`` — the validation-repair re-stream is starting;
#: ``("cost", AuthoringCost)`` — the metered real cost (M3, T2a), emitted once
#: immediately BEFORE the terminal draft (a non-billed pass-through for the client);
#: ``("draft", AuthoringDraft)`` — the single terminal event (validated or errored).
AuthoringStreamEvent = (
    tuple[Literal["chunk", "retry"], str]
    | tuple[Literal["cost"], AuthoringCost]
    | tuple[Literal["draft"], AuthoringDraft]
)

__all__ = [
    "RECOMMENDER_PROMPT_VERSION",
    "AuthoringCost",
    "AuthoringSampling",
    "AuthoringStreamEvent",
    "generate_authoring_draft",
    "recommend_capabilities_for_persona",
    "recommend_tools_for_persona",
    "refine_authoring_draft",
    "stream_authoring_draft",
    "stream_refine_authoring_draft",
]


def _price_usage(
    backend: ChatBackend,
    usage: TokenUsage | None,
    cost_source: CostSource | None,
) -> tuple[float, CostBasis, int, int]:
    """Price one authoring generation's usage → ``(cost_cents, basis, prompt, completion)``.

    Mirrors the chat loop's ``compute_turn_cost`` call (D-M2-1/3): the served
    backend's provider/model + token counts, with an OpenRouter ``usage.cost``
    actual taking precedence. Authoring has no fallback wrapper, so the served
    identity IS the backend's own ``provider_name`` / ``model_name``. ``usage is
    None`` (a backend that reported none) degrades to ``(0.0, "unpriced", 0, 0)``.
    """
    if usage is None:
        return 0.0, "unpriced", 0, 0
    cost, basis = compute_turn_cost(
        provider=backend.provider_name,
        model=backend.model_name,
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        actual_cost_usd=usage.cost_usd,
        source=cost_source,
    )
    return cost, basis, usage.prompt_tokens, usage.completion_tokens


class AuthoringSampling(BaseModel):
    """Sampling knobs for the CREATIVE draft / refinement generation (D-10-3).

    Only the FIRST generation attempt samples at these values — the
    validation-repair retry always runs deterministically (temperature 0.0,
    no top_p/top_k) so raising creativity never weakens the schema-compliance
    contract. ``top_p`` / ``top_k`` of ``None`` leave the provider default
    untouched; ``top_k`` is a no-op on the OpenAI path (see the backends).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    temperature: float = Field(default=0.9, ge=0.0, le=2.0)
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    top_k: int | None = Field(default=None, ge=1)


#: Deterministic sampling used by the validation-repair retry (D-10-3). The
#: retry must reliably FIX schema errors, so it is always greedy regardless of
#: how creative the first attempt was.
_DETERMINISTIC = AuthoringSampling(temperature=0.0, top_p=None, top_k=None)

#: MCP-reference prefix on a unified capability name (``mcp:<server>``).
_MCP_PREFIX = "mcp:"

#: Tool-recommender prompt version (spec 26 T09). Bump when the rubric changes.
RECOMMENDER_PROMPT_VERSION = "v1"
#: Hard cap on returned recommendations (D-26-X-recommender-output-mechanism).
_MAX_RECOMMENDATIONS = 10
#: Confidence floor; weaker "just in case" tools are dropped.
_CONFIDENCE_FLOOR = 0.5


def _validate_yaml(yaml_text: str) -> list[str]:
    """Validate a draft YAML against the v1.0 schema; return error strings ([] = valid).

    Uses placeholder id/owner (the create endpoint assigns the real ones). Both
    YAML-parse failures and Pydantic ``extra="forbid"`` / type failures surface
    as human-readable strings the retry feeds back to the model.
    """
    try:
        raw = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        return [f"invalid YAML: {str(exc)[:200]}"]
    if not isinstance(raw, dict):
        return [f"top-level YAML must be a mapping, got {type(raw).__name__}"]
    raw = dict(raw)
    raw.setdefault("persona_id", "draft")
    raw.setdefault("owner_id", "draft")
    try:
        Persona.model_validate(raw)
    except ValidationError as exc:
        return [
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
            for err in exc.errors(include_url=False)
        ]
    return []


def _retry_feedback(errors: list[str]) -> str:
    """The follow-up message that feeds validation errors back to the model (§3.3)."""
    joined = "\n".join(f"- {e}" for e in errors)
    return (
        "The YAML you produced has the following validation errors:\n"
        f"{joined}\n"
        "Please fix them and return the corrected persona in the same format "
        f"(the YAML, then a line with only {QUESTIONS_MARKER}, then the JSON "
        "questions array). Do not add any field that is not in the schema."
    )


def _finalize_text(text: str) -> tuple[AuthoringDraft, list[str]]:
    """Turn one generation's raw text into ``(draft, errors)``; ``errors == []`` means valid.

    THE single place model text becomes a draft — shared by the blocking
    (:func:`_generate`) and streamed (:func:`_generate_stream`) paths so the
    ``split_response`` → ``_validate_yaml`` contract (D-10-1/3) cannot drift
    between them (D-P0-sse-reuse). The returned draft carries ``errors``
    populated iff validation failed, so an exhausted retry hands back the
    best-effort YAML for the structured form to fix (§3.3) — and the caller
    always has the validated-or-errored draft to deliver (D-P0-errors-on-terminal).
    """
    yaml_text, questions = split_response(text)
    errors = _validate_yaml(yaml_text)
    draft = AuthoringDraft(
        yaml=yaml_text,
        questions=questions,
        prompt_version=AUTHORING_PROMPT_VERSION,
        errors=errors or None,
    )
    return draft, errors


def _retry_messages(
    messages: list[ConversationMessage], prior_content: str, errors: list[str]
) -> list[ConversationMessage]:
    """Append the model's prior output + the validation-error feedback (§3.3)."""
    now = datetime.now(UTC)
    return [
        *messages,
        ConversationMessage(role="assistant", content=prior_content, created_at=now),
        ConversationMessage(role="user", content=_retry_feedback(errors), created_at=now),
    ]


async def _generate(
    backend: ChatBackend,
    messages: list[ConversationMessage],
    sampling: AuthoringSampling,
) -> AuthoringDraft:
    """Run the chat → parse → validate → retry-once loop; return a draft envelope.

    The FIRST attempt samples at ``sampling`` (creative — distinctive names +
    personality). On first-attempt validity, returns immediately. On failure,
    the validation-repair retry runs DETERMINISTICALLY (temperature 0.0) so it
    reliably fixes schema errors regardless of how creative the first attempt
    was (§3.3 / D-10-3). If the retry also fails, returns the best-effort YAML
    with the errors attached (the structured form fixes them) — never raises.
    Shares :func:`_finalize_text` with the streamed path (no contract drift).
    """
    response = await backend.chat(
        messages,
        temperature=sampling.temperature,
        top_p=sampling.top_p,
        top_k=sampling.top_k,
    )
    draft, errors = _finalize_text(response.content)
    if not errors:
        return draft
    # The repair pass is GREEDY (D-10-3): high temp to invent, low temp to fix.
    retry = await backend.chat(
        _retry_messages(messages, response.content, errors),
        temperature=_DETERMINISTIC.temperature,
        top_p=_DETERMINISTIC.top_p,
        top_k=_DETERMINISTIC.top_k,
    )
    # Retry exhausted (still invalid) → _finalize_text attaches the errors so the
    # structured form can fix them. Never raise — a draft is for review.
    draft2, _ = _finalize_text(retry.content)
    return draft2


async def _generate_stream(
    backend: ChatBackend,
    messages: list[ConversationMessage],
    sampling: AuthoringSampling,
    *,
    cost_source: CostSource | None = None,
) -> AsyncIterator[AuthoringStreamEvent]:
    """Stream the same generate-loop as :func:`_generate`, yielding semantic events.

    Yields ``("chunk", delta)`` per text fragment as the model generates, then
    runs the SHARED :func:`_finalize_text` on the accumulated text. On a clean
    first attempt it emits ``("cost", AuthoringCost)`` then the terminal
    ``("draft", AuthoringDraft)`` and ends. On validation failure it emits a
    visible ``("retry", ...)`` and RE-STREAMS the deterministic repair attempt
    (D-P0-restream-retry), then the SUMMED cost (both attempts were paid) and the
    terminal draft — validated, or carrying ``errors`` if the repair also failed
    (D-P0-errors-on-terminal). NEVER a half-formed draft as terminal.

    Spec M3 (T2a): the metered real cost is accumulated across every generation
    the turn made (``_price_usage`` over each attempt's final ``usage``) and
    surfaced ONCE via the ``cost`` event just before the terminal draft, so the
    route can bill the whole authoring turn its real cost (D-M3-9). ``usage``
    rides the final ``StreamChunk`` of each attempt (``is_final=True``).

    Transport-agnostic (D-P0-service-yields-events): frames no SSE; the route
    maps these events to ``text/event-stream`` and deducts only after the
    terminal ``draft`` (so an aborted stream — this generator closed early — or a
    provider error yields no draft/cost and is never charged; D-08-6 lineage).
    """
    total_cents = 0.0
    total_prompt = 0
    total_completion = 0
    basis: CostBasis = "unpriced"

    buffer: list[str] = []
    usage: TokenUsage | None = None
    async for chunk in backend.chat_stream(
        messages,
        temperature=sampling.temperature,
        top_p=sampling.top_p,
        top_k=sampling.top_k,
    ):
        if chunk.delta:
            buffer.append(chunk.delta)
            yield ("chunk", chunk.delta)
        if chunk.usage is not None:
            usage = chunk.usage
    cents, basis, prompt_tok, completion_tok = _price_usage(backend, usage, cost_source)
    total_cents += cents
    total_prompt += prompt_tok
    total_completion += completion_tok
    draft, errors = _finalize_text("".join(buffer))
    if not errors:
        yield _cost_event(total_cents, basis, total_prompt, total_completion)
        yield ("draft", draft)
        return

    # Re-stream the deterministic repair attempt, visibly (D-P0-restream-retry /
    # D-10-3): high temp to invent, low temp to fix. Its cost adds to the turn's.
    yield ("retry", "validation")
    repair: list[str] = []
    repair_usage: TokenUsage | None = None
    async for chunk in backend.chat_stream(
        _retry_messages(messages, "".join(buffer), errors),
        temperature=_DETERMINISTIC.temperature,
        top_p=_DETERMINISTIC.top_p,
        top_k=_DETERMINISTIC.top_k,
    ):
        if chunk.delta:
            repair.append(chunk.delta)
            yield ("chunk", chunk.delta)
        if chunk.usage is not None:
            repair_usage = chunk.usage
    r_cents, r_basis, r_prompt, r_completion = _price_usage(backend, repair_usage, cost_source)
    total_cents += r_cents
    total_prompt += r_prompt
    total_completion += r_completion
    # Same backend both attempts → same basis; keep the repair's when it priced
    # (a genuine basis over a first-attempt ``unpriced`` miss).
    if r_basis != "unpriced":
        basis = r_basis
    draft2, _ = _finalize_text("".join(repair))
    yield _cost_event(total_cents, basis, total_prompt, total_completion)
    yield ("draft", draft2)


def _cost_event(
    cost_cents: float, basis: CostBasis, prompt_tokens: int, completion_tokens: int
) -> tuple[Literal["cost"], AuthoringCost]:
    """Build the ``("cost", AuthoringCost)`` event (the summed authoring-turn cost)."""
    return (
        "cost",
        AuthoringCost(
            cost_cents=cost_cents,
            cost_basis=basis,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        ),
    )


async def generate_authoring_draft(
    backend: ChatBackend,
    description: str,
    available_tools: list[str],
    available_skills: list[str],
    *,
    sampling: AuthoringSampling | None = None,
) -> AuthoringDraft:
    """Generate a draft persona from a description (no row created; D-10-2).

    Args:
        backend: The authoring-tier chat backend (injected; D-10-1).
        description: The user's natural-language persona description.
        available_tools: Tool names to inject so the model only suggests real tools (S10-3).
        available_skills: Skill names to inject.
        sampling: Creative-generation sampling for the FIRST attempt. ``None``
            uses the :class:`AuthoringSampling` default (temperature 0.9). The
            validation-repair retry is always deterministic regardless (D-10-3).

    Returns:
        An :class:`AuthoringDraft` (validated YAML + 2-4 questions + prompt version,
        or best-effort YAML + errors if the retry was exhausted).
    """
    messages = build_authoring_prompt(description, available_tools, available_skills)
    return await _generate(backend, messages, sampling or AuthoringSampling())


async def refine_authoring_draft(
    backend: ChatBackend,
    current_yaml: str,
    question: str,
    answer: str,
    available_tools: list[str],
    available_skills: list[str],
    *,
    sampling: AuthoringSampling | None = None,
) -> AuthoringDraft:
    """Refine a draft by applying the user's answer to a clarifying question (§4).

    Same parse/validate/retry path as :func:`generate_authoring_draft`.

    Args:
        backend: The authoring-tier chat backend.
        current_yaml: The draft YAML being refined.
        question: The clarifying question the user answered.
        answer: The user's answer.
        available_tools: Tool names to inject.
        available_skills: Skill names to inject.
        sampling: Creative-generation sampling for the FIRST attempt. ``None``
            uses the :class:`AuthoringSampling` default. The repair retry stays
            deterministic (D-10-3).

    Returns:
        An updated :class:`AuthoringDraft`.
    """
    messages = build_refinement_prompt(
        current_yaml, question, answer, available_tools, available_skills
    )
    return await _generate(backend, messages, sampling or AuthoringSampling())


# -- streamed variants (spec P0) --------------------------------------------


def stream_authoring_draft(
    backend: ChatBackend,
    description: str,
    available_tools: list[str],
    available_skills: list[str],
    *,
    sampling: AuthoringSampling | None = None,
    cost_source: CostSource | None = None,
) -> AsyncIterator[AuthoringStreamEvent]:
    """Stream a draft persona from a description (spec P0; D-10-2 contract preserved).

    The streamed sibling of :func:`generate_authoring_draft`.
    Yields semantic ``AuthoringStreamEvent``s (chunk / retry / terminal draft);
    the accumulated text runs the SHARED :func:`_finalize_text`, so the streamed
    terminal draft is contract-equivalent to the blocking path (D-10-1/3, no
    drift). No persona row is created (D-10-2). Returns the async iterator
    directly (D-02-5 shape) — iterate it; the route frames events to SSE.

    Args:
        backend: The authoring-tier chat backend (injected; D-10-1).
        description: The user's natural-language persona description.
        available_tools: Tool names to inject (so the model suggests only real tools).
        available_skills: Skill names to inject.
        sampling: Creative-generation sampling for the FIRST attempt. ``None``
            uses the :class:`AuthoringSampling` default. The repair re-stream is
            always deterministic (D-10-3).
        cost_source: The provenance-carrying resolver chain for real-cost pricing
            (Spec M3, T2a); ``None`` uses ``compute_turn_cost``'s zero-network
            static-only default (exact for the Sonnet-class authoring model;
            OpenRouter actuals pass through regardless).
    """
    messages = build_authoring_prompt(description, available_tools, available_skills)
    return _generate_stream(
        backend, messages, sampling or AuthoringSampling(), cost_source=cost_source
    )


def stream_refine_authoring_draft(
    backend: ChatBackend,
    current_yaml: str,
    question: str,
    answer: str,
    available_tools: list[str],
    available_skills: list[str],
    *,
    sampling: AuthoringSampling | None = None,
    cost_source: CostSource | None = None,
) -> AsyncIterator[AuthoringStreamEvent]:
    """Stream a refinement (§4); same event shape + shared finalize as the draft stream.

    Args:
        backend: The authoring-tier chat backend.
        current_yaml: The draft YAML being refined.
        question: The clarifying question the user answered.
        answer: The user's answer.
        available_tools: Tool names to inject.
        available_skills: Skill names to inject.
        sampling: Creative-generation sampling for the FIRST attempt. ``None``
            uses the :class:`AuthoringSampling` default; the repair re-stream
            stays deterministic (D-10-3).
        cost_source: The resolver chain for real-cost pricing (Spec M3, T2a);
            ``None`` uses the static-only default.
    """
    messages = build_refinement_prompt(
        current_yaml, question, answer, available_tools, available_skills
    )
    return _generate_stream(
        backend, messages, sampling or AuthoringSampling(), cost_source=cost_source
    )


# -- tool recommender (spec 26 T09) -----------------------------------------


def _catalog_block() -> str:
    """Render the known-tool catalog as the recommender's candidate list."""
    return "\n".join(f"- {e.name}: {e.description}" for e in TOOL_CATALOG)


def _recommender_messages(description: str) -> list[ConversationMessage]:
    """Build the rubric-based recommender prompt (D-26-X-recommender-output-mechanism).

    The full catalog is enumerated inline; the rubric biases toward precision so
    the model returns a small advertised set — which the tool-count literature
    shows preserves downstream selection accuracy (research R-26-1). The output
    contract is a bare JSON array, validated + catalog-filtered after the call.
    """
    now = datetime.now(UTC)
    system = (
        "You recommend a SMALL set of tools for an AI persona, given its "
        "description. Choose only from this catalog:\n\n"
        f"{_catalog_block()}\n\n"
        "Rules:\n"
        "- Recommend a tool ONLY if the persona's identity, role, or tasks imply "
        "a RECURRING need for it. Prefer precision over recall.\n"
        "- Never recommend a tool 'just in case'. A focused persona needs 3-8 "
        "tools; never more than 10.\n"
        "- A smaller, well-matched set makes the persona MORE accurate at "
        "picking the right tool at runtime — extra tools hurt.\n"
        "- Use ONLY tool names from the catalog above. Do not invent names.\n\n"
        'Output ONLY a JSON array of objects, each {"tool_name": str, '
        '"rationale": str (one line), "confidence": number 0-1}. No prose, no '
        "code fences. Example:\n"
        '[{"tool_name": "web_search", "rationale": "Looks up current case law.", '
        '"confidence": 0.9}]'
    )
    return [
        ConversationMessage(role="system", content=system, created_at=now),
        ConversationMessage(role="user", content=description, created_at=now),
    ]


def _extract_json_array(text: str) -> list[object] | None:
    """Extract the first JSON array from ``text`` (tolerates surrounding prose)."""
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, list) else None


def _parse_recommendations(raw: list[object]) -> list[ToolRecommendation]:
    """Validate raw items → ToolRecommendation, dropping invalid/hallucinated/weak.

    Applies the post-hoc guards (D-26-X-recommender-output-mechanism):
    catalog-membership filter (drop hallucinated names), confidence floor, dedup
    keeping the highest confidence per tool, sort descending, cap at the max.
    """
    catalog = known_tool_names()
    best: dict[str, ToolRecommendation] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            rec = ToolRecommendation.model_validate(item)
        except ValidationError:
            continue
        if rec.tool_name not in catalog or rec.confidence < _CONFIDENCE_FLOOR:
            continue
        existing = best.get(rec.tool_name)
        if existing is None or rec.confidence > existing.confidence:
            best[rec.tool_name] = rec
    ranked = sorted(best.values(), key=lambda r: r.confidence, reverse=True)
    return ranked[:_MAX_RECOMMENDATIONS]


async def recommend_tools_for_persona(
    backend: ChatBackend,
    description: str,
) -> list[ToolRecommendation]:
    """Recommend a ranked tool subset for a persona description (spec 26 T09).

    Uses a single mid-tier call (the route injects the mid backend, D-26-2) with
    a rubric prompt enumerating the known-tool catalog, then validates +
    catalog-filters the result in code. Retries once if the model returns no
    parseable JSON array. Never raises — an unparseable second attempt yields an
    empty list (the authoring form simply shows no recommendations).

    Args:
        backend: The mid-tier chat backend (injected; D-26-2).
        description: The user's natural-language persona description.

    Returns:
        Up to 10 :class:`ToolRecommendation`s, highest-confidence first, each
        with a catalog-valid ``tool_name`` and ``confidence >= 0.5``.
    """
    messages = _recommender_messages(description)
    response = await backend.chat(messages, temperature=0.0)
    raw = _extract_json_array(response.content)
    if raw is None:
        now = datetime.now(UTC)
        retry_messages = [
            *messages,
            ConversationMessage(role="assistant", content=response.content, created_at=now),
            ConversationMessage(
                role="user",
                content="Return ONLY a JSON array as specified — no prose, no code fences.",
                created_at=now,
            ),
        ]
        retry = await backend.chat(retry_messages, temperature=0.0)
        raw = _extract_json_array(retry.content)
        if raw is None:
            return []
    return _parse_recommendations(raw)


# -- unified capability recommender (spec 27 T10, D-26-10 / D-27-13) ---------


def _unified_catalog_block(available_skills: Sequence[str]) -> str:
    """Render the unified candidate list: built-in tools + skills + MCP servers.

    Each candidate carries its provider so the model reasons across providers in
    one ranked pass (D-27-13) rather than per category.
    """
    lines = ["Built-in tools:"]
    lines += [f"- {e.name} [builtin]: {e.description}" for e in TOOL_CATALOG]
    if available_skills:
        lines.append("\nSkills:")
        lines += [f"- {name} [skill]" for name in available_skills]
    lines.append("\nMCP servers (reference as mcp:<name>):")
    for name, entry in BUILTIN_MCP_CATALOG.servers.items():
        lines.append(f"- mcp:{name} [{recommender_provider_tag(entry)}]: {entry.description}")
    return "\n".join(lines)


def _unified_recommender_messages(
    description: str, available_skills: Sequence[str]
) -> list[ConversationMessage]:
    """Build the unified rubric prompt (same precision rubric as Spec 26)."""
    now = datetime.now(UTC)
    system = (
        "You recommend a SMALL set of CAPABILITIES for an AI persona, given its "
        "description. A capability is a built-in tool, a skill, or an MCP server. "
        "Choose only from this catalog:\n\n"
        f"{_unified_catalog_block(available_skills)}\n\n"
        "Rules:\n"
        "- Recommend a capability ONLY if the persona's identity, role, or tasks "
        "imply a RECURRING need for it. Prefer precision over recall.\n"
        "- Recommend AT MOST 10 capabilities TOTAL across all providers; a focused "
        "persona needs 3-8. A smaller, well-matched set makes the persona MORE "
        "accurate at picking the right capability at runtime.\n"
        "- Use ONLY names from the catalog above (MCP servers as 'mcp:<name>'). Do "
        "not invent names.\n\n"
        'Output ONLY a JSON array of objects, each {"name": str, "rationale": str '
        '(one line), "confidence": number 0-1}. No prose, no code fences. Example:\n'
        '[{"name": "web_search", "rationale": "Looks up current case law.", '
        '"confidence": 0.9}, {"name": "mcp:filesystem", "rationale": "Drafts '
        'documents to disk.", "confidence": 0.7}]'
    )
    return [
        ConversationMessage(role="system", content=system, created_at=now),
        ConversationMessage(role="user", content=description, created_at=now),
    ]


def _resolve_capability(name: str, available_skills: frozenset[str]) -> tuple[str, str] | None:
    """Map a model-supplied name to ``(canonical_name, provider)`` or ``None``.

    The provider is DERIVED from the authoritative vocabularies (not trusted from
    the model): built-in tool catalog, the persona's skills, or the MCP catalog.
    An unrecognised name is a hallucination → dropped.
    """
    if name in known_tool_names():
        return name, "builtin"
    if name in available_skills:
        return name, "skill"
    # MCP servers — tolerate both 'mcp:<name>' and a bare '<name>'.
    server = name[len(_MCP_PREFIX) :] if name.startswith(_MCP_PREFIX) else name
    if server in known_mcp_server_names():
        entry = mcp_server_entry(server)
        if entry is not None:
            return f"{_MCP_PREFIX}{server}", recommender_provider_tag(entry)
    return None


def _parse_capability_recommendations(
    raw: list[object], available_skills: frozenset[str]
) -> list[ToolRecommendation]:
    """Validate raw items → provider-tagged ToolRecommendation (D-27-13).

    Drops hallucinations (name not in any provider's vocabulary) + weak entries,
    dedups by canonical name keeping the highest confidence, sorts descending,
    and caps at the COMBINED maximum across all providers (not per category).
    """
    best: dict[str, ToolRecommendation] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        rationale = item.get("rationale")
        confidence = item.get("confidence")
        if not isinstance(name, str) or not isinstance(rationale, str):
            continue
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
            continue
        if confidence < _CONFIDENCE_FLOOR:
            continue
        resolved = _resolve_capability(name, available_skills)
        if resolved is None:
            continue
        canonical, provider = resolved
        try:
            rec = ToolRecommendation(
                tool_name=canonical,
                rationale=rationale,
                confidence=float(confidence),
                provider=provider,
            )
        except ValidationError:
            continue
        existing = best.get(canonical)
        if existing is None or rec.confidence > existing.confidence:
            best[canonical] = rec
    ranked = sorted(best.values(), key=lambda r: r.confidence, reverse=True)
    return ranked[:_MAX_RECOMMENDATIONS]


async def recommend_capabilities_for_persona(
    backend: ChatBackend,
    description: str,
    *,
    available_skills: Sequence[str] = (),
) -> list[ToolRecommendation]:
    """Recommend a unified, provider-tagged capability set (spec 27 T10).

    The D-26-10 generalisation of :func:`recommend_tools_for_persona`: one
    mid-tier call ranks built-in tools, skills, and MCP servers TOGETHER against
    the same precision rubric, validated + provider-resolved in code, capped at
    the COMBINED maximum (D-27-13). Never raises — an unparseable second attempt
    yields an empty list.

    Args:
        backend: The mid-tier chat backend (injected; D-26-2).
        description: The user's natural-language persona description.
        available_skills: Skill ids the platform offers (the recommender's skill
            vocabulary). Empty ⇒ skills are simply not recommended.

    Returns:
        Up to 10 provider-tagged :class:`ToolRecommendation`s across all
        providers, highest-confidence first.
    """
    skill_vocab = frozenset(available_skills)
    messages = _unified_recommender_messages(description, available_skills)
    response = await backend.chat(messages, temperature=0.0)
    raw = _extract_json_array(response.content)
    if raw is None:
        now = datetime.now(UTC)
        retry_messages = [
            *messages,
            ConversationMessage(role="assistant", content=response.content, created_at=now),
            ConversationMessage(
                role="user",
                content="Return ONLY a JSON array as specified — no prose, no code fences.",
                created_at=now,
            ),
        ]
        retry = await backend.chat(retry_messages, temperature=0.0)
        raw = _extract_json_array(retry.content)
        if raw is None:
            return []
    return _parse_capability_recommendations(raw, skill_vocab)
