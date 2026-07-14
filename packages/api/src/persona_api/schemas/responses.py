"""Response models for the hosted API (spec 08, T06).

Pydantic v2 models that shape the JSON the API returns + the SSE event payloads.
Clean and explicit so FastAPI's OpenAPI spec (the web app's TS-client source,
spec 09) is well-formed — optional fields are properly nullable, the SSE
payloads serialise via ``model_dump_json`` straight into ``data:`` lines.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — Pydantic needs it at runtime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

__all__ = [
    "AcceptanceCriterionOut",
    "ApprovalDecisionResult",
    "ApprovalOut",
    "ArtifactItem",
    "ArtifactListResponse",
    "BudgetExtendResult",
    "BudgetOut",
    "GrantOut",
    "LedgerOut",
    "TaskAuditEntryOut",
    "TaskCheckpointOut",
    "TaskCommandResult",
    "TaskDetailOut",
    "TaskReportOut",
    "TaskSummaryOut",
    "ArtifactMetadataView",
    "AuthoringDraft",
    "ChunkEvent",
    "ClarifyingQuestion",
    "ConversationDetail",
    "ConversationSummary",
    "CreditsResponse",
    "DoneEvent",
    "MemoryEvolutionEntry",
    "MemoryLinkEdge",
    "MemoryLinkView",
    "MemoryNodeDetail",
    "MemoryNodeSummary",
    "MemoryProvenanceView",
    "MemorySearchResponse",
    "MemorySearchResult",
    "MemoryWindowResponse",
    "MessageView",
    "NavCountsResponse",
    "PersonaCapabilities",
    "PersonaDetail",
    "PersonaSummary",
    "RunStatusResponse",
    "ToolCallEvent",
    "ToolRecommendation",
    "ToolRecommendationResponse",
    "ToolResultEvent",
    "ToolSummary",
    "UsageEntry",
    "UserProfileResponse",
]


class _Output(BaseModel):
    """Base for responses: reject unknown fields so we never leak stray data."""

    model_config = ConfigDict(extra="forbid")


# -- personas ---------------------------------------------------------------


class PersonaSummary(_Output):
    """A persona in a list view (no full YAML).

    Spec 35: the library card surfaces a capability + identity glance. The
    counts below are parsed from the SAME stored YAML the list query already
    loads (so they cost nothing extra), and ``conversation_count`` is a single
    GROUP-BY over the RLS-scoped conversations — not an N+1.
    """

    id: str
    name: str
    role: str
    avatar_url: str | None = None
    created_at: datetime
    updated_at: datetime
    # Spec 35 — capability/identity glance for the library card (all free):
    language: str = "en"
    # "Apps & tools": the persona's tool allow-list length, which already folds
    # MCP servers (a persona enables a server by carrying `mcp:<name>` in its
    # `tools` list — see persona-form.tsx), so built-in tools + MCP count as one.
    tools_count: int = 0
    skills_count: int = 0
    constraints_count: int = 0
    conversation_count: int = 0


class PersonaCapabilities(_Output):
    """Deployment-derived capability flags surfaced with the persona detail.

    Hydrated from the runtime :class:`persona_runtime.tier.TierRegistry` so the
    UI can answer "does this persona support image attachments?" BEFORE the
    user attempts to send (Spec 13 fail-loud made visible — Spec F3 §10 #7;
    D-F3-X-no-vision-surface-shape). At v0.1 the answer is deployment-wide:
    every persona under a given deployment shares the same registry, so
    ``vision`` is identical across personas — see D-F3-X-deployment-vs-persona-
    capability-framing. The field's shape survives the v0.2 inflection where
    per-persona tier pins make the answer genuinely per-persona; only the
    hydration source changes (from registry to per-persona lookup).

    Attributes:
        vision: ``True`` iff at least one configured tier resolves to a
            backend whose ``supports_vision`` is ``True``. Read via the
            public :meth:`TierRegistry.supports_vision_for` method
            (D-F3-X-tier-registry-public-contract).
        configured_tiers: Tier names registered on the active deployment
            in insertion order (``("small", "mid", "frontier")`` for the
            typical three-tier deployment). The UI may surface these in a
            disabled-attach tooltip to explain *which* models the deployment
            has configured.
    """

    vision: bool
    configured_tiers: tuple[str, ...]


class PersonaMemoryItem(BaseModel):
    """One graph memory attributed to a persona (the page's memories modal)."""

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    content: str
    created_at: datetime
    #: The originating conversation when the provenance carries one (deep link).
    conversation_id: str | None = None


class PersonaMemoriesResponse(BaseModel):
    """R11-B6 — a persona's graph memories, newest first (capped; honest total)."""

    model_config = ConfigDict(frozen=True)

    available: bool
    total: int
    items: list[PersonaMemoryItem]


class AvatarRegenerateResult(BaseModel):
    """R11-B6 — the avatar-regeneration acknowledgement (async either way)."""

    model_config = ConfigDict(frozen=True)

    queued: bool


class PersonaDetail(_Output):
    """A persona's full detail (YAML + metadata).

    The optional :attr:`capabilities` field (D-F3-X-capability-endpoint) is
    additive on top of the Spec 08 / Spec 09 surface: tests + composition
    roots that do not wire a :class:`TierRegistry` (e.g. unit fixtures
    without the runtime) omit the field and the API returns ``None`` so the
    persona-detail surface stays usable without runtime composition.
    """

    id: str
    yaml: str
    schema_version: str
    avatar_url: str | None = None
    # Spec R3 (R3-D-2 / R3-D-4 / EU AI Act Art. 50): synthetic-media provenance for
    # the avatar. ``avatar_source`` is the structural signal stored on the row
    # (``'generated'`` / ``'uploaded'`` / ``None`` = unknown for legacy rows).
    # ``avatar_ai_generated`` is the *derived* recipient-facing disclosure the web
    # renders an "AI-generated" badge from: True when generated, False when uploaded,
    # None when unknown — derived from the stored signal, never guessed. Additive;
    # both default None so legacy rows + unit fixtures stay byte-identical.
    avatar_source: str | None = None
    avatar_ai_generated: bool | None = None
    capabilities: PersonaCapabilities | None = None
    # Spec 21 T09 (D-21-7): tri-state auto-dispatch consent surfaced to the
    # settings UI. None = never asked / revoked-to-ask, True = granted,
    # False = declined. Additive — omitted defaults to None on legacy rows.
    consent_to_auto_dispatch: bool | None = None
    consent_updated_at: datetime | None = None
    # Spec N2 (N2-D-4 surface c): MCP servers this persona has enabled
    # (``mcp:<name>`` in its ``tools``) that are no longer in the available catalog —
    # e.g. the auto-sync removed them upstream. Additive; empty for the common case.
    # The owner-visible signal that an enabled capability disappeared (vs vanishing
    # silently); the live tool-call path already degrades without crashing (§7.3).
    unavailable_mcp_servers: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    # Spec 35: conversations the persona has had — the source of its episodic
    # memory (retrieved + compacted per turn at runtime). One cheap COUNT; lets
    # the detail's episodic-store card show a real figure instead of a guess.
    conversation_count: int = 0
    # R11-B6 riders — the persona page's at-a-glance, honest counts: tasks ever
    # created for this persona, and graph memories attributed to it (0 when the
    # deployment has no graph store). Additive with defaults.
    tasks_run_count: int = 0
    memory_count: int = 0


# -- LLM-assisted authoring (spec 10, §3 / D-10-6) --------------------------


class ClarifyingQuestion(_Output):
    """One suggested question the user can answer to improve a draft persona.

    ``section`` is a free-form hint (expected: identity | self_facts | worldview
    | constraints | tools | skills) — NOT an enum, so a model that names a
    section we don't anticipate doesn't sink the parse.
    """

    section: str
    question: str


class AuthoringDraft(_Output):
    """The draft envelope returned by ``/author`` and ``/author/refine`` (D-10-2).

    A draft is NOT a persona row — the user reviews/refines it, then saves via
    ``POST /v1/personas`` (which creates the row). ``errors`` is populated only
    when validation retries are exhausted (best-effort YAML returned for the form
    to fix, §3.3); ``None`` on success.
    """

    yaml: str
    questions: list[ClarifyingQuestion] = Field(default_factory=list)
    prompt_version: str
    errors: list[str] | None = None


class ToolRecommendation(_Output):
    """One recommended capability for a persona (spec 26 T09 / spec 27 T10).

    Spec 27 realises the D-26-10 unification: the same shape now carries a
    provider tag so built-in tools, skills, and MCP servers rank together. The
    ``provider`` field defaults to ``"builtin"`` so the Spec-26 shape (and its
    callers/tests) stay a forward-compatible strict subset.

    Attributes:
        tool_name: The capability name — a built-in tool name from
            ``persona.tools.TOOL_CATALOG``, a skill id, or an ``mcp:<server>``
            reference. Hallucinated names are filtered out post-hoc.
        rationale: One-line reason the capability fits this persona.
        confidence: Recommender confidence in [0, 1]; entries below the floor
            are dropped before return.
        provider: Where the capability comes from — ``"builtin"`` (tool),
            ``"skill"``, ``"mcp:builtin"`` (default-enabled MCP server), or
            ``"mcp:optional"`` (opt-in / BYO MCP server). The UI groups by
            provider but ranks across all (spec 27 §2.3 / D-27-13).
    """

    tool_name: str
    rationale: str
    confidence: float = Field(ge=0.0, le=1.0)
    provider: str = "builtin"


class ToolRecommendationResponse(_Output):
    """The ranked tool-recommendation list returned by ``/personas/recommend-tools``."""

    recommendations: list[ToolRecommendation] = Field(default_factory=list)
    prompt_version: str


# -- conversations ----------------------------------------------------------


class ConversationSummary(_Output):
    """A conversation in a list view.

    The two ``last_message_*`` fields let the sidebar render a real preview of
    the most recent turn instead of falling back to the title. They are
    populated in a single set-based LIST query (a ``ROW_NUMBER()`` window over
    the RLS-scoped ``messages`` rows — no per-row fan-out) and are ``None`` for
    a conversation that has no messages yet.

    Attributes:
        id: The conversation id.
        persona_id: The persona this conversation belongs to.
        title: The conversation's display title.
        created_at: Creation timestamp (UTC-aware).
        updated_at: Last-activity timestamp (UTC-aware); list order is by this
            field descending.
        last_message_preview: The most recent message's text, trimmed and
            truncated server-side to :data:`LAST_MESSAGE_PREVIEW_MAX_LEN`
            characters (an ellipsis replaces the tail when it overflows).
            ``None`` when the conversation has no messages.
        last_message_role: Speaker role of the most recent message, using the
            existing message-role vocabulary (``user`` is the human; every
            other role is the persona/assistant side). ``None`` when the
            conversation has no messages. The UI switches on this to attribute
            the preview ("You: …" vs the persona).
    """

    id: str
    persona_id: str
    title: str
    origin: Literal["chat", "call"] = "chat"
    created_at: datetime
    updated_at: datetime
    last_message_preview: str | None = None
    last_message_role: Literal["user", "assistant", "system", "tool"] | None = None


class MessageView(_Output):
    """A single message in a conversation history."""

    id: str
    role: str
    content: str
    created_at: datetime
    # Opaque connector passthrough (D-08-3); null for web-UI messages.
    channel: dict[str, object] | None = None
    # Spec 35 D-35-2: the routing tier this assistant turn used, persisted so the
    # per-message tier chip renders on a reloaded conversation. Null on
    # user/system/tool rows and on assistant rows written before migration 010
    # (the chip degrades to "no chip" — never a wrong tier).
    tier_used: str | None = None
    # Spec P3 (P3-D-1/2): the assistant turn's persisted ordered rich event log,
    # projected from the ``messages.stream_events`` column (P1's checkpoint, made
    # durable at terminal by ``MessagesTurnSink.finalize``). The frontend
    # reconstructs the interleaved view (text spans + tool_call/tool_result cards +
    # artifact refs) from it — see ``persistedToView``. Reusing P1's ONE log means
    # no second source of truth (P3-D-1); the field is named ``events`` to decouple
    # the API vocabulary from the column's P1-internal "checkpoint" name (P3-D-2).
    # The shape is the persisted hybrid — RunEvent dumps (``{type, step, data,
    # timestamp}``) interleaved with text deltas (``{kind, delta}``) — hand-mirrored
    # in the web ``persistedToView`` mapper, the same as the SSE shapes (D-09-1;
    # OpenAPI can't model it, so it is a loose dict-list pinned by the contract
    # test, mirroring ``ActiveTurnResponse.stream_events``). **Null** on
    # user/system/tool rows and on legacy/non-streamed assistant rows (NULL
    # column) → byte-exact text-only render (criterion 5; the ``tier_used``
    # nullable-additive precedent).
    events: list[dict[str, object]] | None = None


class ConversationDetail(_Output):
    """Full conversation history."""

    id: str
    persona_id: str
    title: str
    # Spec V9 V9-D-3: the immutable birth-origin marker ('chat' | 'call'). Default
    # 'chat' for the pre-V9 / pre-marker degrade (every historical conversation).
    origin: Literal["chat", "call"] = "chat"
    messages: list[MessageView]
    created_at: datetime
    updated_at: datetime


class CallSummary(_Output):
    """A finished (or in-progress) voice call in the Calls history (Spec V9, V9-D-5).

    The durable call envelope read from the ``calls`` table (V9-D-3: the
    call-record is the Calls-membership key, NOT ``origin``). ``conversation_id``
    wires each call to its saved transcript — the spoken turns now persist as
    ``messages`` (V9-D-1/D-2), so ``GET /v1/conversations/{conversation_id}``
    renders them under the same thread UI as a text chat.

    Attributes:
        call_id: The call-record id.
        conversation_id: The conversation this call ran on — the transcript link.
        persona_id: The persona on the call (the web resolves the display name /
            avatar, as it does for ``ConversationSummary``).
        title: The call's transcript-derived title (R9-028) — the SAME
            ``conversations.title`` column ``ConversationSummary`` carries
            (joined in, the ``calls`` table has no title column of its own).
            ``''`` until R9-020's title machinery (turn-end threshold or the
            R9-028 voice session-end leg) has run at least once.
        started_at: When the call went active (UTC-aware); list order is by this
            field descending.
        ended_at: When the call ended; ``None`` while live / on a crash.
        duration_s: Stored whole-second duration; ``None`` until the call ends.
        end_reason: Why the call ended; ``None`` while live.
    """

    call_id: str
    conversation_id: str
    persona_id: str
    title: str = ""
    started_at: datetime
    ended_at: datetime | None = None
    duration_s: int | None = None
    end_reason: Literal["user_hangup", "switched", "error", "disconnect"] | None = None


# -- SSE chat events (§5.2) -------------------------------------------------


class ChunkEvent(_Output):
    """``event: chunk`` — an incremental delta of the assistant's response."""

    delta: str
    is_final: bool = False


class ToolCallEvent(_Output):
    """``event: tool_call`` — the model invoked a tool."""

    tool: str
    args: dict[str, object] = Field(default_factory=dict)
    # Spec 30 T01 (D-30-1): the call's source badge — ``builtin`` / ``skill`` /
    # ``mcp:builtin`` / ``mcp:optional``. Optional for OpenAPI/back-compat parity
    # with the additive wire field; absent on pre-spec-30 frames.
    kind: str | None = None


class ToolResultEvent(_Output):
    """``event: tool_result`` — a tool's result (D-03-3: is_error + content)."""

    tool: str
    content: str
    is_error: bool = False
    # Spec 30 T01 (D-30-1): the call's source badge (see ToolCallEvent.kind).
    kind: str | None = None


class RoutingSummary(_Output):
    """Spec 31 (D-31-1) — concise model-decision summary on the ``done`` event.

    Additive; present only on intelligent-routing turns. The raw score vector is
    NOT here — it stays on the JSONL TurnLog. The web templates the localized
    "why" phrase from these structured/enum fields (``dominant_factor`` is the
    single highest-weighted axis the chosen model won on).
    """

    chosen_model: str
    dominant_factor: str | None = None
    model_fallback_engaged: bool = False
    model_fallback_reason: str | None = None


class BudgetSnapshot(_Output):
    """Spec 31 (D-31-2) — per-session budget snapshot for the budget indicator.

    Additive; present only when intelligent routing is on and a cap is set.
    ``session_spent_cents`` includes the just-completed turn (read post-turn).
    Caps are omitted when unset; ``max_cents_per_day`` is surfaced when set so
    the UI can show 23's configured-but-deferred fail-loud honestly.
    """

    session_spent_cents: float
    max_cents_per_turn: float | None = None
    max_cents_per_session: float | None = None
    max_cents_per_day: float | None = None


class DoneEvent(_Output):
    """``event: done`` — the terminal event.

    ``format_hints`` (D-08-3) is the connector echo channel: empty ``{}`` from
    the API; connectors populate/interpret it themselves (spec 12).
    """

    usage: dict[str, int] = Field(default_factory=dict)
    tier: str
    format_hints: dict[str, str] = Field(default_factory=dict)
    # Spec 31 — additive, SEPARATE routing (D-31-1) + budget (D-31-2) fields.
    # Both omitted on rule-based turns / when no cap is set (back-compat).
    routing: RoutingSummary | None = None
    budget: BudgetSnapshot | None = None


# -- runs (§5.3) ------------------------------------------------------------


class RunStatusResponse(_Output):
    """A run's status + its accumulated steps (JSON-serialised Run/Step)."""

    id: str
    persona_id: str
    task: str
    status: str
    steps: list[dict[str, object]] = Field(default_factory=list)
    output: str | None = None
    error: str | None = None


class RunSummary(_Output):
    """A run in the Tasks index — a light projection without the steps JSON."""

    id: str
    persona_id: str
    task: str
    status: str
    started_at: datetime
    finished_at: datetime | None = None


class RunListResponse(_Output):
    """The caller's runs, newest first (Spec 35 Tasks page index)."""

    items: list[RunSummary] = Field(default_factory=list)


class ActiveTurnResponse(_Output):
    """The in-progress assistant turn for a conversation (Spec P1 reattach surface).

    Returned by ``GET /conversations/{id}/active-turn`` so the web client detects
    a live turn on return and seeds the partial — the accumulated ``content`` plus
    the tool/text interleave in ``stream_events`` (the persisted checkpoint shape)
    — before resubscribing to the live tail at ``…/active-turn/events``. A 404
    means there is no active turn (all messages are terminal). ``stream_events``
    is the DB checkpoint shape, NOT the core ``ConversationMessage`` model (the
    byte-for-byte dump corpus is untouched).
    """

    message_id: str
    streaming_status: str
    content: str
    stream_events: list[dict[str, object]] = Field(default_factory=list)


class TurnIntoFileResponse(_Output):
    """202 acknowledgment for "Turn into file" (R9-025b) — a durable job reference.

    The file itself is NOT ready yet — it lands (or the job dead-letters) some
    seconds later, out of band; the client's Files surface picks it up on its
    own refresh/poll (see ``file_extract``'s module docstring for the exact
    refresh-signal decision). ``job_id`` is ``None`` only on the (safe,
    idempotent) duplicate-enqueue path — the SAME (message, format) job is
    already queued/running from an earlier click.
    """

    job_id: str | None
    status: Literal["queued"]


# -- credits / usage (§5.5) -------------------------------------------------


class CreditsResponse(_Output):
    """The user's current credit balance (stub counter).

    ``low_balance`` is True when the balance is below
    :data:`credits_service.LOW_BALANCE_THRESHOLD` (10 000 by default) — the web
    app uses it to surface the under-limit warning (D-11-12).
    """

    balance: int
    low_balance: bool = False


class UsageEntry(_Output):
    """One usage-log row (per-turn telemetry, paginated).

    Spec M2 (D-M2-4) additive fields — the web can label estimates vs actuals:

    * ``cost_basis`` — how ``cost_cents`` was derived: ``"actual_openrouter"``
      (the OpenRouter response's own cost — what we actually paid),
      ``"estimate_static"`` / ``"estimate_catalog"`` (resolver-chain
      estimates), ``"unpriced"`` (no data; 0.0). ``None`` = legacy pre-M2
      row — render as an estimate.
    * ``pricing_source`` — constant ``"unified"`` marker: rows are priced by
      the unified Spec-22/23 source (per-row so the list response shape is
      unchanged — no envelope break for generated clients).
    """

    persona_id: str | None = None
    tier_used: str
    model_name: str
    prompt_tokens: int
    completion_tokens: int
    cost_cents: float
    cost_basis: str | None = None
    pricing_source: Literal["unified"] = "unified"
    created_at: datetime


# -- profile (Spec K6, K6-D-1) ----------------------------------------------


class UserProfileResponse(_Output):
    """The caller's own profile — identity anchor + optional name (Spec K6).

    ``first_name`` / ``last_name`` are optional (``None`` when unset — a nameless
    account is fully valid). ``email`` may be ``None`` when the token carried none
    (the provisioning fallback stores a noreply address, but the surface stays
    nullable so the contract never implies a real address). Our DB is the source of
    truth; Clerk stays auth-only.
    """

    id: str
    email: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    #: The caller's per-user IANA timezone (Spec A8, A8-D-9). ``None`` when unset —
    #: schedule computation + rendering then fall back to ``PERSONA_DEFAULT_TIMEZONE``.
    timezone: str | None = None
    #: Per-user quiet hours (Spec A8, A8-D-6): local minutes-of-day [start, end) in the
    #: user's timezone. Both ``None`` = off (off-until-set).
    quiet_hours_start: int | None = None
    quiet_hours_end: int | None = None
    #: The caller's sticky last-choice model preference (Spec M1, M1-T6) — the
    #: OpenRouter model id last picked in the web model picker. ``None`` when
    #: unset, so the runtime loop falls back to the tier-resolved default.
    preferred_model: str | None = None
    created_at: datetime


class NavCountsResponse(_Output):
    """The owner-scoped totals behind the sidebar nav badges (R9-010).

    One cheap round-trip for every nav-row count — each field is an
    index-friendly ``COUNT`` over the caller's own rows (RLS-scoped like the
    sibling ``/v1/me`` routes):

    - ``personas`` / ``conversations``: honest TOTALS (the sidebar previously
      derived these from its truncated preview lists). ``conversations``
      counts chat-born threads only (``origin != 'call'`` — call transcripts
      belong to the Calls surface).
    - ``calls``: all call records.
    - ``memory_nodes``: canonical knowledge-graph nodes (``merged_into IS
      NULL``, matching what the Memory surface shows); ``0`` when no graph
      store is wired (the Memory nav row is hidden then anyway).
    - ``active_tasks``: the active working set — non-terminal states only
      (from :data:`persona.tasks.state.TERMINAL_STATES`'s complement), never
      completed/failed/cancelled history.
    - ``schedules``: schedule ROWS — a recurring schedule counts once, never
      its occurrences/fires.
    """

    personas: int
    conversations: int
    calls: int
    memory_nodes: int
    active_tasks: int
    schedules: int


class NotificationOut(_Output):
    """One durable bell notification (Spec P6 feed), owner-scoped + RLS.

    Copy is locale-neutral (P6-D-5): the web resolves ``message_key`` + ``params``
    via next-intl at render. ``kind`` + ``ref_id`` drive the deep-link
    (``run_terminal`` → ``/runs/{ref_id}``, ``persona_ready`` → ``/personas/{ref_id}``).
    """

    id: str
    kind: str
    ref_id: str | None = None
    level: str
    message_key: str
    params: dict[str, str] = {}
    read: bool
    created_at: datetime


class NotificationMarkReadResult(_Output):
    """How many feed rows a mark-read touched (0 = nothing unread / not owned)."""

    updated: int


# -- connectors (Spec C6) ---------------------------------------------------


class ConnectorConnectionOut(_Output):
    """One active platform connection (Spec C6), owner-scoped + RLS.

    Returned by ``GET /v1/me/connectors`` — the caller's live bindings only;
    absence of a platform ⇒ not connected. ``platform_identity`` is the bound
    envelope: a phone number / email address (human-recognisable) or an opaque
    platform user id (Telegram/Discord/Slack numeric id) — the web formats it
    per platform. No token or secret is ever exposed here (only the public
    identity + when it linked).
    """

    platform: str
    platform_identity: str
    linked_at: datetime


class ConnectorDisconnectResult(_Output):
    """The outcome of a disconnect (Spec C6, DELETE ``…/connectors/{p}/{id}``).

    ``severed`` is ``True`` iff an active binding of the caller was revoked;
    ``False`` is the **idempotent no-op** — the binding was already disconnected,
    never existed, or isn't the caller's (RLS hides a foreign binding, so it is a
    no-op, never a ``404`` that would leak whether it exists). Disconnect is
    idempotent by design: a repeat is a clean ``severed=false``.
    """

    severed: bool


class ConnectorLinkArtifact(_Output):
    """A link-initiation artifact (Spec C6, POST ``…/connectors/{platform}/link``).

    The front-door normalizes the connector service's issue response into ONE shape the
    web renders, regardless of mechanism: exactly one of ``deep_link`` (Telegram),
    ``authorize_url`` (Discord/Slack OAuth), or ``code`` (WhatsApp/SMS/email OTP) is set;
    ``destination`` accompanies ``code`` for the reversed flow ("text/email it to …",
    C6-D-7); ``expires_at`` is the server-authoritative token expiry (C6-D-8) the web's
    countdown + re-issue key off. ``extra="forbid"`` guarantees no token, secret, or stray
    upstream field can ride along — the front-door copies only these known keys.
    """

    deep_link: str | None = None
    authorize_url: str | None = None
    code: str | None = None
    destination: str | None = None
    expires_at: datetime


# -- tools / skills (§5.4) --------------------------------------------------


class ToolSummary(_Output):
    """A tool or skill name + description (read-only listing)."""

    name: str
    description: str


class SpecialitySummary(_Output):
    """A speciality (skill) catalog entry with its trust tier + version handle (Spec S3).

    The user-facing "Specialities" surface renders skills with their source-assigned
    trust tier (S1-D-3 — never self-declared) and binds consent to ``content_hash``
    (S1-D-5: a synced body change → new hash → prior consent is stale → re-gate).
    ``requires_consent`` is the enablement gate (S1-D-4: ``community``/``third_party``
    need owner consent before injection; ``builtin``/``vetted`` activate freely).
    """

    name: str
    description: str
    when_to_use: str | None = None
    #: SkillTrust value — builtin | vetted | community | third_party.
    trust: str
    #: True for community/third_party (S1-D-4); drives the consent flow.
    requires_consent: bool
    #: sha256 of the SKILL.md body — the version handle consent binds to (S1-D-5).
    content_hash: str | None = None
    #: Provenance (source-assigned, S1-D-3) — "builtin" or an S2 source id, + repo/commit.
    source: str | None = None
    source_uri: str | None = None
    source_ref: str | None = None


class PersonaSpecialitySummary(SpecialitySummary):
    """A speciality plus THIS persona's consent state (Spec S3, S3-D-3).

    The persona-scoped surface adds the server-computed ``consent_state`` — the one
    security-authoritative bit the client cannot derive (it needs the consent store +
    the current hash). ``not_required`` (builtin/vetted), ``granted`` (consented at the
    current hash), ``stale`` (consented at an old body hash → re-gate, S1-D-5), or
    ``none`` (never/revoked → default-deny). Enablement (the ``skills:`` declaration)
    and ``unavailable`` stay client-derived from the edited persona draft.
    """

    #: not_required | granted | stale | none (skill_consent_service).
    consent_state: str


# -- artifacts (Spec F5 D-F5-1) ---------------------------------------------


class ArtifactMetadataView(_Output):
    """Sidecar metadata surfaced through the artifact list endpoint.

    Mirrors ``services.artifact_metadata.WorkspaceArtifactMetadata`` at the
    API surface. Kept as a distinct response model (rather than re-exporting
    the service shape) so the OpenAPI schema is self-contained and the
    web client gets stable types.
    """

    source: str
    # Spec R3 (R3-D-4 / EU AI Act Art. 50): the *derived* recipient-facing
    # disclosure — True when ``source == 'generated'``, False when ``'uploaded'``,
    # None when unknown. Rides the existing structural ``source`` signal (not a
    # duplicate); the web renders an "AI-generated" badge from this. Additive.
    ai_generated: bool | None = None
    type: str
    producing_spec: str
    conversation_id: str | None
    created_at: datetime
    original_name: str | None


class ArtifactItem(_Output):
    """A single workspace artifact in the F5 list view.

    The ``ref`` is the workspace-relative path the existing
    ``GET /v1/personas/{id}/uploads/{ref}`` route already knows how to
    serve — F5 reuses that route for downloads + inline rendering.
    """

    ref: str
    size_bytes: int
    media_type: str
    metadata: ArtifactMetadataView | None = None


class ArtifactListResponse(_Output):
    """Paginated artifact-list response for D-F5-1.

    ``total`` is the post-filter count; ``items`` is the window of size
    ``limit`` starting at ``offset``. The client computes ``hasMore`` from
    ``offset + items.length < total``.
    """

    total: int
    limit: int
    offset: int
    items: list[ArtifactItem]


class MCPServerDetail(_Output):
    """A bring-your-own MCP server as returned to its owner (spec 30, D-30-3).

    The credential is NEVER included — only ``has_credential`` (whether one is
    stored). ``discovered_tools`` is the cached eager-discovery result (D-30-5),
    ``None`` until a successful test-connection.
    """

    id: str
    name: str
    url: str
    auth_method: str
    enabled: bool
    has_credential: bool
    discovered_tools: list[str] | None = None
    # Adoption provenance (Spec N4, N4-D-9): the catalog entry a self-extension adoption
    # came from (e.g. ``notion-remote``), or ``None`` for a manually-added BYO server.
    # Display metadata for the "self-extended" marker — never a secret.
    catalog_source: str | None = None
    # Spec R8: the OAuth provider key (e.g. ``github``) for an ``oauth`` server, else
    # ``None``. Display metadata (drives the "Connect / Reconnect" affordance); never a
    # secret. Token presence is conveyed by ``has_credential`` (true once authorized).
    oauth_provider: str | None = None
    created_at: datetime
    updated_at: datetime


class MCPConnectionStatus(_Output):
    """One assigned MCP server's connection status (Spec N6, N6-D-6; R4-C1-21).

    Makes "assigned" visibly distinct from "working": ``connected`` ⇒ the server's tools
    reach the model; otherwise ``reason`` names why (the T1 vocabulary — ``starting`` /
    ``spawn_failed`` / ``stopped`` / ``fly_outage`` / ``no_key`` / ``unvetted`` /
    ``runtime_capacity`` / ``not_enabled``). The web renders ``reason`` as a friendly badge —
    never the raw enum. No secret is ever included.
    """

    server_name: str
    connected: bool
    reason: str | None = None


class MCPOAuthAuthorizeResponse(_Output):
    """The provider authorize URL to redirect the user to (Spec R8, T4).

    ``authorize_url`` carries the PKCE ``code_challenge`` + opaque ``state`` — no
    secret. The flow completes at the web callback → ``POST /mcp-servers/oauth/callback``.
    """

    authorize_url: str


class MCPOAuthCallbackResponse(_Output):
    """Result of completing an OAuth flow (Spec R8, T5).

    ``server`` is the now-connected server (``has_credential`` true). ``redirect_after``
    is the server-side-stored app path to return the user to (or ``None``).
    """

    server: MCPServerDetail
    redirect_after: str | None = None


class MCPServerTestResult(_Output):
    """Outcome of a BYO-MCP test-connection (spec 30, D-30-5).

    ``ok`` true → ``tools`` lists the discovered tool names (cached on the row).
    ``ok`` false → ``error`` is a short, non-sensitive reason category.
    """

    ok: bool
    tools: list[str] = Field(default_factory=list)
    error: str | None = None


class MCPCatalogSecret(_Output):
    """A credential an MCP server requires — DISPLAY-ONLY schema (Spec N1, D-N1-5).

    Carries **no value field by construction**: the catalog API exposes WHICH secret a
    server needs (so the apps UX can render the setup form), never a secret value. The
    credential isolation property (user → secret store → Gateway, never an LLM turn) is
    upheld at the API boundary, not just internally.
    """

    name: str
    env: str
    example: str = ""
    description: str = ""


class MCPCatalogServer(_Output):
    """An MCP server in the management catalog (spec 30 T11 + N1).

    A persona enables a server by adding ``mcp:<name>`` to its ``tools``
    allow-list. ``provider`` is the recommender tag (``mcp:builtin`` /
    ``mcp:optional``); ``required_env`` lists env vars an operator must set.

    The N1 fields below carry the Docker catalog-mirror display metadata the apps UX
    (N3) renders. They are **additive-with-default** so the existing five-field
    contract is unchanged — a client written against spec 30 sees no break.
    """

    name: str
    description: str
    provider: str
    default_enabled: bool
    required_env: list[str] = Field(default_factory=list)
    # -- N1 (D-N1-3): Docker catalog-mirror display metadata (additive-with-default) --
    display_name: str = ""
    icon_url: str = ""
    image: str = ""
    server_type: str = "builtin"
    risk: str = "low"
    source_project: str = ""
    source_commit: str = ""
    signed: bool = False
    allow_hosts: list[str] = Field(default_factory=list)
    secrets: list[MCPCatalogSecret] = Field(default_factory=list)
    # -- Spec R8/N7: per-user OAuth binding passthrough (additive-with-default) --
    # Mirrors ``MCPServerCatalogEntry.auth_method``/``oauth_provider`` (Spec R8,
    # R8-D-7) so the web catalog card can offer a Connect affordance (N7-T3) instead
    # of a credential form for an ``auth_method == "oauth"`` entry. Empty = the
    # pre-R8/N7 env/credential/none path (every entry before the github rebind).
    auth_method: str = ""
    oauth_provider: str = ""


class MCPDeploymentCapabilities(_Output):
    """Which MCP mechanisms THIS deployment can actually run (Spec N7, D-N7-2).

    Rides the ``/v1/mcp-catalog`` response so the web can render an honest,
    deployment-truthful surface instead of guessing from indirect signals:

    Attributes:
        per_tenant_runtime: ``True`` iff the N6 per-tenant image-MCP runtime is
            composed on THIS deployment (cloud + operator ack + configured) — the
            exact condition under which adopting a ``server_type == "server"``
            (image) catalog app will actually spawn a Machine. ``False`` means an
            image-app adopt would have nothing to run on.
        gateway: ``True`` iff a Docker MCP Gateway URL is configured
            (``PERSONA_DOCKER_MCP_GATEWAY_URL``) — an operator MAY have exposed an
            image-type app there even though this deployment has no per-tenant
            runtime; the web renders that as an operator-managed note rather than a
            hard "unavailable" (the catalog can't see what the operator enabled on
            the gateway).
        oauth_providers: The configured pre-registered OAuth provider keys (Spec
            R8's ``provider_registry`` — e.g. ``["github"]``), for the BYO manager's
            fail-closed provider select (N7-T3b). The generic ``mcp-native``
            (auto-discovery, DCR) path needs no operator config and is always
            offered client-side regardless of this list.
    """

    per_tenant_runtime: bool
    gateway: bool
    oauth_providers: list[str] = Field(default_factory=list)


class MCPCatalogResponse(_Output):
    """The wrapped ``GET /v1/mcp-catalog`` response (Spec N7, D-N7-2).

    A bare array cannot carry a sibling field, so the deployment capabilities ride
    alongside the server list in one wrapper object (a breaking response-shape
    change, landed atomically with the web's unwrap in the SAME commit, N7-T2).
    ``servers`` is additionally filtered server-side to what THIS deployment can
    actually offer (the C-filter, N7 owner ruling: an image-type app with no
    runtime and no gateway is excluded outright rather than listed-then-refused —
    see ``catalog_service.is_listable``).
    """

    servers: list[MCPCatalogServer]
    capabilities: MCPDeploymentCapabilities


# -- K5: Memory (the knowledge-graph UI) ------------------------------------
# Pure projection of the K0 graph types (ConceptNode / TypedLink / NodeProvenance)
# — no graph logic here, the API just shapes what the store returns (criterion 12).
# ``kind`` (NodeKind) and ``link_type`` (LinkType) are strings, not Literals, so a
# future store-side enum value never breaks the contract.


class MemoryProvenanceView(_Output):
    """Where a memory came from — the structured basis the UI renders as story."""

    source: str
    persona_id: str | None = None
    # Human-readable name of the persona that learned this (for the panel avatar).
    # ``None`` until resolved from ``persona_id`` (R-K5-PROV-PERSONA follow-up); the
    # UI falls back to a source-based avatar when absent.
    persona_name: str | None = None
    interaction_id: str | None = None
    # The conversation this memory can be opened at (``/chat/{conversation_id}``).
    # Set ONLY when the source interaction IS a conversation (chat/voice); ``None`` for
    # a run-sourced memory, whose ``interaction_id`` is a run id and would 404 the link
    # (R-K5-OPEN-CONV). The UI enables "open conversation" iff this is present.
    conversation_id: str | None = None
    written_at: datetime
    reason: str | None = None
    grounding: str | None = None


class MemoryEvolutionEntry(_Output):
    """One step in how a memory grew — a provenance contribution (oldest first)."""

    source: str
    written_at: datetime
    reason: str | None = None
    superseded_content: str | None = None


class MemoryNodeSummary(_Output):
    """A node as drawn on the canvas — no content/provenance (that is the detail).

    ``degree`` is the node's connectedness within the returned window (0 when not
    computed for this view) — the "size by connectedness, lightly" signal (K5-D-3).
    """

    id: str
    kind: str
    label: str
    wellbeing_category: str | None = None
    degree: int = 0


class MemoryLinkEdge(_Output):
    """A typed edge for the canvas — one of the four LinkType relationships."""

    src_node_id: str
    dst_node_id: str
    link_type: str
    weight: float | None = None


class MemoryWindowResponse(_Output):
    """A windowed slice of the graph — the seed (no focus) or a focus neighbourhood.

    Never the whole graph (K5-D-2): ``total_nodes`` is the owner's full tally for the
    header; ``nodes``/``links`` are only the loaded window.

    ``available`` distinguishes *no graph store* (this deployment has no usable graph —
    e.g. community-on-SQLite, the K0 graph being Postgres-only) from *an empty graph*
    (a real but as-yet-unpopulated map). The UI must not show the "no memories yet"
    invite when the truth is "Memory isn't available here" — so the nav is gated and
    the page shows a distinct unavailable state when this is ``False`` (Spec K5).
    """

    available: bool = True
    focus_id: str | None = None
    is_seed: bool
    total_nodes: int
    nodes: list[MemoryNodeSummary]
    links: list[MemoryLinkEdge]


class MemoryLinkView(_Output):
    """A traversable typed link in the detail panel — the edge plus the neighbour."""

    link_type: str
    weight: float | None = None
    direction: Literal["out", "in"]
    neighbor: MemoryNodeSummary


class MemoryNodeDetail(_Output):
    """A node's full detail: content, provenance-as-story, evolution, typed links."""

    id: str
    kind: str
    label: str
    content: str
    wellbeing_category: str | None = None
    created_at: datetime
    origin: MemoryProvenanceView
    evolution: list[MemoryEvolutionEntry]
    links: list[MemoryLinkView]


class MemorySearchResult(_Output):
    """One search hit — exact-term and paraphrase ranks both visible (K1 hybrid)."""

    node_id: str
    label: str
    kind: str
    score: float
    dense_rank: int | None = None
    sparse_rank: int | None = None


class MemorySearchResponse(_Output):
    """The matches for a Memory search query, best-first (criterion 5)."""

    query: str
    results: list[MemorySearchResult]


class ApprovalOut(_Output):
    """One pending approval, rendered FAITHFULLY for the A6 inbox (criterion 5).

    The proposal's exact ``arguments`` + ``description`` are returned VERBATIM — the inbox is a
    safety surface, not a summary (approving a paraphrase would approve a different action). The
    web client renders them as TEXT, never HTML (XSS-safe: an email body is untrusted content).
    """

    proposal_id: str
    task_id: str
    persona_id: str
    tool_name: str
    #: The EXACT recorded payload (email body, amount, payee, …) — verbatim, never paraphrased.
    arguments: dict[str, JsonValue]
    description: str
    categories: list[str]
    created_at: datetime
    #: ``created_at`` + the 72h expiry — the inbox's countdown (the sweep auto-pauses past it).
    expires_at: datetime


class ApprovalDecisionResult(_Output):
    """The result of an inbox decision — the outcome + the DURABLE post-state (A6-D-3).

    ``status`` is read back from the durable A3 record after resolution, so a chat-vs-inbox race is
    honest: the loser gets ``outcome=None`` / ``note="not_pending"`` while ``status`` shows what
    actually won (``approved``/``denied``/…). The surface reflects 'already handled', never errors.
    """

    outcome: str | None  # approve|deny|modify|clarify, or null on an idempotent no-op
    executed: bool
    note: str
    status: str  # the durable ProposalStatus after resolution — the reflection anchor


# --- Spec A6 (B1): the task read surfaces (list / detail / audit) --------------------------


class GrantOut(_Output):
    """One row of "what you authorised" — a category and its effective decision (grants visible)."""

    category: str  # observe / spend / communicate_as_user / …
    decision: str  # allow | gate | deny


class AcceptanceCriterionOut(_Output):
    id: str
    statement: str
    status: str


class LedgerOut(_Output):
    """The cost ledger, per kind + total (µ-dollars)."""

    model_micros: int
    sandbox_micros: int
    external_micros: int
    total_micros: int


class BudgetOut(_Output):
    """Spend against the effective cap (contract bound + any extensions)."""

    cap_micros: int
    spent_micros: int
    state: str  # ok | approaching | reached


class TaskCheckpointOut(_Output):
    """A checkpoint rendered as the human "where it is / what's next" (never raw transcripts)."""

    seq: int
    progress_conclusions: list[str]
    next_step: str
    open_questions: list[str]
    blocked_on: str | None
    updated_at: datetime


class TaskReportOut(_Output):
    """The terminal outcome — a distinct projection so a failure never renders as a success."""

    kind: str  # completed | stuck | cancelled
    cause: str = ""  # stuck: the honest why
    conclusions: list[str] = []  # completed: the findings
    where_it_stood: list[str] = []  # stuck / cancelled
    next_step: str = ""


class TaskSummaryOut(_Output):
    """One task in the cross-persona list — state, spend, and whether it's paused (the matrix)."""

    task_id: str
    persona_id: str
    goal: str
    status: str  # IntrospectionStatus (just_created/progressing/waiting_on_user/scheduled/…)
    paused: bool
    spent_micros: int
    budget_cap_micros: int
    updated_at: datetime
    #: The wait/blocked reason for a stuck task — the head checkpoint's ``blocked_on`` (A6-D-5),
    #: populated only for the waiting_on_user/failed subset so the list is loud-WITH-information.
    #: ``None`` for every non-stuck row (additive to the B1 contract).
    stuck_cause: str | None = None


class TaskDetailOut(_Output):
    """The task, above the run viewer: contract + grants, state, ledger, budget, report, waits."""

    task_id: str
    persona_id: str
    goal: str
    scope: str
    status: str
    paused: bool
    grants: list[GrantOut]  # the contract's category policy — what you authorised
    acceptance_criteria: list[AcceptanceCriterionOut]
    deadline: datetime | None
    max_legs: int | None
    budget: BudgetOut
    ledger: LedgerOut
    progress: list[str]  # the human "where it is" (checkpoint conclusions)
    next_step: str
    open_questions: list[str]
    wait_reason: str | None
    report: TaskReportOut | None  # present once terminal
    checkpoints: list[TaskCheckpointOut]  # recent, human-readable
    conversation_id: str | None
    schedule_id: str | None
    run_ids: list[str]  # the Spec 08 runs the leg timeline drills into
    created_at: datetime
    updated_at: datetime


class TaskAuditEntryOut(_Output):
    """One audit-trail row for a task (budget / lifecycle / trigger provenance), readable."""

    action: str
    target: str
    metadata: dict[str, JsonValue]
    created_at: datetime


class TaskCommandResult(_Output):
    """The durable result of a task command (pause/resume/cancel) — reflect, never error (B2)."""

    task_id: str
    status: str  # the durable IntrospectionStatus after the command
    paused: bool
    changed: bool  # false = an idempotent no-op (already in the target state)
    #: True on a resume while the owner's autonomy is paused — resumed the overlay, but it won't
    #: run until autonomy resumes (reflect the pause, never silently arm).
    owner_autonomy_paused: bool = False
    note: str = ""  # an honest server-side note (the in-flight fate / the no-op reason)


class BudgetExtendResult(_Output):
    """The result of a budget extension — bounded, at-most-once, with the old → new cap (B2)."""

    task_id: str
    applied: bool  # false = a clean no-op (not budget-paused, or the extend race lost)
    old_cap_micros: int
    new_cap_micros: int
    state: str  # ok | approaching | reached
    note: str = ""


class AutonomyStateOut(_Output):
    """The owner's autonomy-pause state — the durable presence read, reflect never error (B4).

    ``paused`` is read from the ``owner_autonomy_pause`` row (RLS-scoped). ``changed`` is false on
    an idempotent no-op — pausing an already-paused owner, or resuming one who isn't paused.
    """

    paused: bool  # the durable presence: True iff an owner_autonomy_pause row exists
    changed: bool = False  # false = an idempotent no-op (already in the target state)
    note: str = ""  # an honest server-side note (the no-op reason / the suspend-all reach)


class PersonaSuspensionOut(_Output):
    """A single persona's autonomy-suspension state — presence-based, reflect never error (B4).

    Rides the existing ``suspended_personas`` mechanism (the same row ``is_runnable`` consults).
    ``suspended`` is the durable presence read (RLS-scoped); ``changed`` is false on an idempotent
    no-op — suspending an already-suspended persona, or resuming one that isn't suspended.
    """

    persona_id: str
    suspended: bool  # the durable presence: True iff a suspended_personas row exists
    changed: bool = False  # false = an idempotent no-op (already in the target state)
    note: str = ""  # an honest server-side note (the no-op reason)


class InitiativeDialOut(_Output):
    """A persona's initiative restraint level — the durable dial, honest about the flag (B4).

    ``dial`` is the durable ``personas.initiative_dial`` (reflected, never assumed). ``changed`` is
    false when the requested level already matched. ``initiative_enabled`` mirrors the platform
    ``PERSONA_INITIATIVE_ENABLED`` flag: the level persists regardless, but when it is false the UX
    must be honest that initiative won't act until it is enabled.
    """

    persona_id: str
    dial: str  # off | propose_only | act_within_envelope (the durable level)
    changed: bool = False  # false = an idempotent no-op (already at the requested level)
    initiative_enabled: bool = False  # the platform flag — honest UX, not a second gate
    note: str = ""


class InitiativeDeclineOut(_Output):
    """A declined initiative opportunity — user-level, LEDGER-anchored, reflect never error (B4).

    Anchored on the durable A5 ledger notice (never conversation metadata). A decline suppresses the
    opportunity for ALL personas until an explicit revival. ``changed`` is false when the topic was
    already live-declined (an idempotent calm no-op).
    """

    notice_id: str
    opportunity_key: str
    declined: bool  # the durable outcome: the opportunity is suppressed
    changed: bool = False  # false = already live-declined (nothing added)
    note: str = ""
