"""Per-turn telemetry — TurnLog and the writer port (T06).

Every completed turn produces one :class:`TurnLog` (spec §7) recording the tier,
model, token usage, latency, estimated cost, tool-call count, skill used, and
whether history compaction fired. The log is written through a
:class:`TurnLogWriter` port: :class:`JSONLTurnLogWriter` for the CLI/local path,
:class:`MemoryTurnLogWriter` for tests. The hosted service (spec 08) implements
the same port against the Postgres ``turn_logs`` table.

`TurnLog` is a frozen **Pydantic** model, not a ``@dataclass`` (D-05-9): it
crosses the API/Postgres boundary and needs ``model_dump_json`` + tz-aware
datetime validation, following the D-02-2 / D-03-3 precedent.

Cost: since Spec M2 (D-M2-1) the per-turn ``cost_cents`` / ``cost_basis`` are
computed by :func:`persona_runtime.cost.compute_turn_cost` over the Spec-22/23
metadata resolver chain (static tables + OpenRouter catalog) — the
hand-maintained ``_PRICE_TABLE`` that lived in this module was deleted. This
module keeps the TurnLog shape, the writer ports, and refusal detection.
"""

from __future__ import annotations

import re
import threading
from datetime import datetime  # noqa: TC003 — Pydantic needs runtime access for TurnLog.timestamp
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from persona_runtime.routing import (
    RoutingDecision,  # noqa: TC001 — Pydantic model field needs runtime reference
)

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "JSONLTurnLogWriter",
    "MemoryTurnLogWriter",
    "SkillInvocation",
    "TurnLog",
    "TurnLogWriter",
    "detect_tool_refusals",
]


class SkillInvocation(BaseModel):
    """One ``use_skill`` activation record for the TurnLog (Spec 24, D-24-10).

    Full call record — name + parameters + injected-content size — for the
    skill-invocation audit trail. JSONL-only telemetry; never columnar (the
    Postgres writer maps a fixed field subset, so no migration).

    Attributes:
        name: The activated skill.
        parameters: The ``parameters`` object the model passed to ``use_skill``
            (``None`` when none were supplied).
        content_tokens: Tokens of skill content injected for this activation.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    parameters: dict[str, Any] | None = None
    content_tokens: int = Field(default=0, ge=0)


class TurnLog(BaseModel):
    """Telemetry for one completed turn (spec §7).

    Frozen + ``extra="forbid"``. ``timestamp`` must be tz-aware (UTC), matching
    the spec-01 model convention.

    Spec M2 (D-M2-2) — SEMANTIC CORRECTION to :attr:`model_name` /
    :attr:`provider` / :attr:`cost_cents`: they now describe the model that
    ACTUALLY SERVED the turn. Pre-M2, a fallback-engaged turn carried the
    multi-model wrapper's PRIMARY identity here (the wrapper's
    ``provider_name`` / ``model_name`` report the primary by design) — so the
    persisted row, ``/v1/me/usage``, and the cost estimate all named a model
    that never ran. Post-M2 they agree with :attr:`tier_model_chosen` /
    :attr:`tier_provider_used` (which are now redundant-on-success duplicates,
    kept additive-safe for existing consumers); primary-only turns are
    byte-identical.

    M2 review (finding I2) — MULTI-ROUND COVERAGE for :attr:`prompt_tokens` /
    :attr:`completion_tokens` / :attr:`cost_cents`: a turn that ran the tool
    sub-loop for N rounds (each a SEPARATE billed request on OpenRouter) has
    every round summed by :func:`persona_runtime.loop._aggregate_round_usage`
    before this row is built — pre-fix these fields carried only the LAST
    round's numbers (last-writer-wins), understating a heavy tool-use turn's
    true tokens/cost by up to N-1 rounds' worth. :attr:`cost_basis` stays
    ``"actual_openrouter"`` only when EVERY usage-carrying round reported an
    actual; a turn where some rounds did and some fell back mid-turn to a
    non-OpenRouter model drops the partial actual and prices the SUMMED
    tokens as an estimate instead (never a part-actual mislabelled as the
    whole). Attribution is a SEPARATE axis, UNCHANGED by this fix — per the
    paragraph above, :attr:`model_name` / :attr:`provider` describe only the
    FINAL round's served pair even though the token/cost fields cover every
    round. Single-round turns (still the overwhelming majority) are
    byte-identical.

    Spec 18 (T12; D-18-X-turnlog-extension) extends the shape additively with
    routing observability:

    * :attr:`routing_decision` — the full :class:`RoutingDecision` from
      :meth:`Router.route`; serialises as nested JSON when persisted.
    * :attr:`routing_latency_ms` — wall-clock duration of the router's
      decision (from :func:`time.perf_counter` around the :meth:`route`
      call). Distinct from :attr:`latency_ms` which covers the whole turn.
    * :attr:`routing_fallback_triggered` — whether the :class:`UnifiedRouter`
      smart path fell back to the embedded :class:`HeuristicRouter`.
    * :attr:`routing_fallback_reason` — one of ``"timeout"`` /
      ``"scoring_error"`` / ``"empty_metadata"`` / ``"partial_metadata:<tier>"``
      when ``routing_fallback_triggered`` is ``True`` (D-18-X-fallback-instrumentation).

    All four routing fields are OPTIONAL — pre-Spec-18 callers (legacy tests
    that construct TurnLog directly without routing data) stay green by
    omitting them.

    Spec 20 (T12; D-20-5) extends the shape additively with content-hash-only
    reasoning observability:

    * :attr:`reasoning_total_tokens` — count of tokens spent on reasoning
      content during this turn (provider-reported when available; otherwise
      ``None``). Distinct from :attr:`completion_tokens` which excludes the
      reasoning trace for providers that account separately.
    * :attr:`reasoning_text_hash` — sha256 hex digest of the concatenated
      reasoning text emitted by the stream. NEVER persist the raw text
      (content-hash-only per the D-15-X-hard-line-filter precedent —
      reasoning may contain PII or jailbreak attempts the prompt builder
      filtered out). ``None`` if the provider emitted no reasoning.

    Both reasoning fields are OPTIONAL and default to ``None``; pre-Spec-20
    callers stay green by omitting them.

    Spec 20 (T19; D-20-9) extends the shape additively with multi-model
    fallback instrumentation. Operators consume these via the JSONL TurnLog
    stream to identify fallback-rate hot-spots; MAINTENANCE.md D-20-7 row
    triggers operator investigation when fallback-rate > N% / 7-day window.

    * :attr:`tier_model_chosen` — the actual model that successfully returned
      (``None`` when the wrapper exhausted all backends with
      :class:`AllModelsFailedError`).
    * :attr:`tier_provider_used` — provider whose backend served the turn
      (e.g. ``"nvidia"`` / ``"anthropic"``). ``None`` when
      ``AllModelsFailedError`` surfaced.
    * :attr:`tier_fallback_count` — how many backends attempted before
      success. ``0`` means primary served cleanly. Equal-length invariant
      with the reasons / providers lists.
    * :attr:`tier_fallback_reasons` — per-fallback error class name (e.g.
      ``"RateLimitError"``, ``"BackendTimeoutError"``,
      ``"ProviderCredentialMissingError"``). **Class names ONLY**, never
      error message text or context dict values (mirror the
      D-15-X-hard-line-filter content-hash-only-audit privacy precedent).
    * :attr:`tier_fallback_providers` — per-fallback provider name
      (operator-visible signal for cross-provider routing patterns).
      Index ``i`` describes the same fallback attempt as
      ``tier_fallback_reasons[i]``.
    * :attr:`fallback_engaged` — derived operator-convenience: ``True`` iff
      ``tier_fallback_count > 0``. Explicit field (NOT Pydantic
      ``computed_field``) so it serialises cleanly to JSONL and dashboards
      can aggregate without computing.

    All six fallback fields are OPTIONAL; pre-Spec-20 callers stay green
    by omitting them. The :meth:`_validate_fallback_invariants` after-
    validator enforces (a) length-match between count + reasons + providers
    and (b) ``fallback_engaged == (tier_fallback_count > 0)``.

    Spec 25 (T11; §2.9, D-18-1 NOT reopened) extends the shape additively
    with tool-refusal observability:

    * :attr:`tool_refusal_detected` — the list of tool names the model
      refused to use that WERE available for the turn. Populated by
      :func:`detect_tool_refusals` (conservative, low-false-positive). An
      empty list means no refusal-of-available-tool pattern was detected.
      Operators aggregate this over the JSONL stream to surface per-model /
      per-tool refusal rates and drive model-selection guidance
      (spec §2.9 MAINTENANCE.md row). This is OBSERVABILITY ONLY — no
      auto-retry or system-message injection is performed here (that is the
      separate risky T21 surface).

    The field is OPTIONAL and defaults to ``[]``; pre-Spec-25 callers stay
    green by omitting it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    conversation_id: str
    turn_index: int = Field(ge=0)
    tier_used: str
    model_name: str
    provider: str
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    latency_ms: float = Field(ge=0.0)
    cost_cents: float = Field(ge=0.0)
    tool_calls: int = Field(default=0, ge=0)
    skill_used: str | None = None
    history_compacted: bool = False
    timestamp: datetime
    routing_decision: RoutingDecision | None = None
    routing_latency_ms: float = Field(default=0.0, ge=0.0)
    routing_fallback_triggered: bool = False
    routing_fallback_reason: str | None = None
    # Spec 20 T12 (D-20-5): content-hash-only reasoning observability.
    reasoning_total_tokens: int | None = Field(default=None, ge=0)
    reasoning_text_hash: str | None = None
    # Spec 20 T19 (D-20-9) — multi-model fallback instrumentation. Additive
    # per D-05-9. Operators consume these via the JSONL TurnLog stream;
    # MAINTENANCE.md D-20-7 row triggers operator investigation when the
    # fallback-rate metric exceeds N% over a 7-day window. Class-name-only
    # privacy discipline mirrors D-15-X-hard-line-filter.
    tier_model_chosen: str | None = None
    tier_provider_used: str | None = None
    tier_fallback_count: int = Field(default=0, ge=0)
    tier_fallback_reasons: list[str] = Field(default_factory=list)
    tier_fallback_providers: list[str] = Field(default_factory=list)
    fallback_engaged: bool = False
    # Spec 25 T11 (§2.9; D-18-1 NOT reopened) — tool-refusal observability.
    # Tool names the model refused to use that WERE available this turn,
    # detected conservatively by :func:`detect_tool_refusals`. Observability
    # only: NO auto-retry / system-message injection here (that is T21).
    tool_refusal_detected: list[str] = Field(default_factory=list)
    # Spec M2 (D-M2-1; supersedes the Spec 25 D-25-7 two-value vocabulary).
    # ``cost_basis`` records how ``cost_cents`` was derived:
    # "actual_openrouter" (the OpenRouter response's own ``usage.cost`` — what
    # we actually paid), "estimate_static" (Spec-23 static per-provider table,
    # vendor-published), "estimate_catalog" (OpenRouter catalog, derived), or
    # "unpriced" (no data; cost recorded 0.0 — never a guess). Populated by
    # the turn loop from :func:`persona_runtime.cost.compute_turn_cost`. Kept
    # ``str`` (not Literal) so legacy JSONL rows ("published" /
    # "verify-at-deploy") still validate on read; the default is the honest
    # unknown — an unset basis must not claim a price existed.
    cost_basis: str = Field(default="unpriced")
    # Spec 25 T12 (§2.1 / D-25-5/6; D-18-1 NOT reopened) — chronic-fallback
    # alert. ``True`` on every turn while the runtime turn-loop's rolling
    # 10-turn fallback-rate window is in the ALERTING state (>30% = ≥4/10).
    # The window lives in the turn loop (D-25-X-t12-window-location), not here.
    fallback_rate_alert: bool = False
    # Spec 25 T21 (§2.9 RISKY half; default-OFF behind
    # ``PERSONA_REFUSAL_RETRY_ENABLED``) — ``True`` when the turn loop detected
    # a tool-refusal on an available tool and injected ONE corrective
    # system message + re-generated. Always ``False`` when the flag is off
    # (the observability-only path, T11/T12).
    refusal_retry_engaged: bool = False
    # Spec 25 T22 (§2.4; D-25-4; D-18-1 NOT reopened) — ``True`` when any
    # ``code_execution`` call this turn auto-recreated a killed sandbox session
    # (the tool wrapper's retry-once recovery). The wrapper surfaces it per-call
    # in ``ToolResult.metadata["sandbox_session_recreated"]``; the turn loop ORs
    # it across the turn's tool dispatches into this field.
    sandbox_session_recreated: bool = False
    # Spec 24 (D-24-10) — skill-invocation telemetry. ``skills_invoked`` is the
    # ordered chain of skills activated this turn (full call records: name +
    # params + injected size); ``skill_budget_exceeded`` flags a turn where a
    # composed skill was skipped because the shared per-turn skill budget was
    # exhausted (D-24-X-budget-exhaustion-policy). Runtime-only JSONL fields —
    # the Postgres writer maps a fixed columnar subset, so NO migration.
    skills_invoked: list[SkillInvocation] = Field(default_factory=list)
    skill_budget_exceeded: bool = False
    # Spec 26 T10/T12 (D-26-X-T12-turnlog-no-migration) — tool-gap telemetry.
    # ``tool_gap_detected``: catalog tool names the model signalled it lacked
    # this turn (runtime tool-gap detector). ``tool_consent_granted``: tool
    # names the user enabled mid-conversation via the consent flow (populated by
    # the API consent path; default empty on the chat-loop write). Runtime-only
    # JSONL fields — the columnar Postgres writer maps a fixed subset, so these
    # need NO migration.
    tool_gap_detected: list[str] = Field(default_factory=list)
    tool_consent_granted: list[str] = Field(default_factory=list)
    # Spec 27 T12 (D-27-X-no-migration) — MCP telemetry. ``mcp_invocations``: the
    # ``mcp:<server>:<tool>`` names dispatched this turn. ``mcp_unavailable_requested``:
    # catalog MCP server names the model signalled it lacked (runtime MCP-gap
    # detector, T11). Runtime-only JSONL fields — same no-migration discipline as
    # the Spec-26 tool-gap fields above (the columnar writer maps a fixed subset).
    mcp_invocations: list[str] = Field(default_factory=list)
    mcp_unavailable_requested: list[str] = Field(default_factory=list)

    @field_validator("timestamp", mode="after")
    @classmethod
    def _timestamp_must_be_tz_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            msg = "naive datetime not allowed on TurnLog.timestamp"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _validate_fallback_invariants(self) -> TurnLog:
        """Enforce the two T19 D-20-9 invariants on the fallback fields.

        1. ``len(tier_fallback_reasons) == len(tier_fallback_providers) ==
           tier_fallback_count`` — every counted fallback has a matching
           class-name + provider pair, with consistent index ordering.
        2. ``fallback_engaged == (tier_fallback_count > 0)`` — the derived
           bool is not allowed to drift from the count.
        """
        if (
            len(self.tier_fallback_reasons) != self.tier_fallback_count
            or len(self.tier_fallback_providers) != self.tier_fallback_count
        ):
            msg = (
                "tier_fallback_count must equal len(tier_fallback_reasons) and "
                f"len(tier_fallback_providers); got count={self.tier_fallback_count}, "
                f"reasons={len(self.tier_fallback_reasons)}, "
                f"providers={len(self.tier_fallback_providers)}"
            )
            raise ValueError(msg)
        if self.fallback_engaged != (self.tier_fallback_count > 0):
            msg = (
                "fallback_engaged must equal (tier_fallback_count > 0); "
                f"got fallback_engaged={self.fallback_engaged}, "
                f"tier_fallback_count={self.tier_fallback_count}"
            )
            raise ValueError(msg)
        return self


@runtime_checkable
class TurnLogWriter(Protocol):
    """Port for persisting a :class:`TurnLog`. Write-only (CQS)."""

    def write(self, log: TurnLog) -> None:
        """Persist one turn log. No return value (CQS)."""
        ...


class JSONLTurnLogWriter:
    """Appends one JSON object per turn to ``<root>/<conversation_id>.jsonl``.

    Mirrors the spec-01 audit-log path convention (D-01-6 / D-05-10): co-located
    with the data, env-overridable via ``PERSONA_TURNLOG_PATH``. Single-process
    safe via a lock; the line-append itself is atomic enough at persona scale.
    Hosted multi-process safety lands with the Postgres writer in spec 08.
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._lock = threading.Lock()

    def write(self, log: TurnLog) -> None:
        """Append ``log`` as one JSON line under the conversation's file."""
        self._root.mkdir(parents=True, exist_ok=True)
        payload = log.model_dump_json()
        path = self._root / f"{log.conversation_id}.jsonl"
        with self._lock, path.open("a", encoding="utf-8") as f:
            f.write(payload)
            f.write("\n")


class MemoryTurnLogWriter:
    """In-memory writer for tests (mirrors spec-03's MemoryToolAuditLogger)."""

    def __init__(self) -> None:
        self.logs: list[TurnLog] = []

    def write(self, log: TurnLog) -> None:
        """Record ``log`` in insertion order."""
        self.logs.append(log)


# Spec M2 (D-M2-1): the hand-maintained ``_PRICE_TABLE`` / ``_COST_BASIS``
# estimator (S05-3 / D-05-10 / D-25-7) that lived here was DELETED — it was the
# fourth, orphaned price table (no MAINTENANCE.md cadence, no ``openrouter``
# keys, placeholder numbers diverging 2-15x from the quarterly-reviewed Spec-23
# tables). Turn cost now comes from
# :func:`persona_runtime.cost.compute_turn_cost` over the Spec-22/23 metadata
# resolver chain; the coverage this table provided was folded into the static
# tables (``persona.backends.metadata`` — incl. new anthropic current-gen +
# groq rows, M2-T1).


# Spec 25 T11 (§2.9) — tool-refusal detection patterns.
#
# Each entry maps ONE builtin tool name to the regexes that signal the model
# refused that tool's capability. A refusal is only counted when the tool is
# ALSO in ``available_tools`` (the model would be *accurate* refusing an
# unavailable capability — §2.9 distinguishes accurate self-report from a
# training-time refusal-override of a wired tool).
#
# Discipline: CONSERVATIVE / low-false-positive. Every pattern anchors the
# refusal verb ("I can't / I cannot / I'm unable to") DIRECTLY to the
# capability verb+object so that affirmative sentences ("I can generate
# images") and unrelated text never match. Patterns are pre-compiled once,
# case-insensitive (``re.IGNORECASE``). Contractions allow a straight or
# curly apostrophe. Tool names are the canonical builtin names
# (``generate_image`` / ``code_execution`` / ``web_search`` / ``web_fetch``).
#
# This is the OBSERVABILITY half of §2.9 only — detection feeds the
# ``TurnLog.tool_refusal_detected`` field. The affirmative tool-description
# rewrites and any in-flight correction (auto-retry / system-message
# injection) are separate, riskier surfaces (T09/T10/T21).

# Refusal lead-in: "I can't" / "I cannot" / "I am/I'm unable to" / "can not".
# Trailing ``.{0,40}?`` is a short non-greedy bridge so small filler words
# ("really", "currently", "for you") between the verb and the capability
# object still match without spanning unrelated sentences.
_REFUSAL_LEAD = (
    r"i\s*(?:can['’]?t|can\s?not|cannot|am\s+unable\s+to|['’]m\s+unable\s+to)"
    r"[^.?!]{0,40}?"
)

# Capability-object fragments (each anchored to ``_REFUSAL_LEAD``). Verbs kept
# tight to avoid catching affirmative ("I can generate images") or unrelated
# prose. Defined as separate constants so each line stays readable + short.
_IMG_OBJ = (
    r"(?:generate|create|make|produce|draw)\s+"
    r"(?:an?\s+)?(?:images?|pictures?|photos?|illustrations?|drawings?)"
)
_FETCH_OBJ = (
    r"(?:browse|access|fetch|retrieve|open)\s+(?:the\s+)?"
    r"(?:web|internet|live\s+websites?|web\s?sites?|urls?|pages?)"
)
_SEARCH_OBJ = (
    r"(?:browse|access|search)\s+(?:the\s+)?"
    r"(?:web|internet|live\s+websites?|web\s?sites?|online)"
)
_SEARCH_OBJ_ALT = r"search\s+(?:the\s+)?(?:web|internet|online|for\s+(?:current|live|real-time))"
_CODE_OBJ = r"(?:run|execute)\s+(?:code|scripts?|programs?|python)"


def _refusal(capability_object: str) -> re.Pattern[str]:
    """Compile a case-insensitive refusal pattern for ``capability_object``."""
    return re.compile(_REFUSAL_LEAD + capability_object, re.IGNORECASE)


_TOOL_REFUSAL_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "generate_image": (_refusal(_IMG_OBJ),),
    "web_fetch": (_refusal(_FETCH_OBJ),),
    "web_search": (_refusal(_SEARCH_OBJ), _refusal(_SEARCH_OBJ_ALT)),
    "code_execution": (_refusal(_CODE_OBJ),),
}


def detect_tool_refusals(model_output: str, available_tools: list[str]) -> list[str]:
    """Flag available tools the model's text refused to use (Spec 25 §2.9).

    Pure function. Scans ``model_output`` for conservative refusal patterns
    ("I can't / I cannot / I'm unable to <capability>") and returns the
    canonical names of tools whose capability was refused AND that are present
    in ``available_tools``. A refusal for a capability whose tool is *not*
    available yields nothing (the model is reporting accurately). Non-refusal
    text yields the empty list.

    The result is de-duplicated and ordered by ``available_tools`` so the
    output is deterministic for a given allow-list. No retry or correction is
    performed — this is observability only (the corrective half is T21).

    Args:
        model_output: The model's natural-language turn output (assistant
            text). May be empty.
        available_tools: Canonical tool names available to the model this turn
            (the persona's effective allow-list).

    Returns:
        Canonical tool names the model refused despite their availability,
        de-duplicated and ordered by ``available_tools``. Empty when no
        available-tool refusal pattern matched.
    """
    if not model_output or not available_tools:
        return []
    available = set(available_tools)
    detected: list[str] = []
    for tool_name in available_tools:
        if tool_name in detected:
            continue
        patterns = _TOOL_REFUSAL_PATTERNS.get(tool_name)
        if patterns is None or tool_name not in available:
            continue
        if any(pattern.search(model_output) for pattern in patterns):
            detected.append(tool_name)
    return detected
