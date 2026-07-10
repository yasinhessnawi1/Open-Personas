"""Request models for the hosted API (spec 08, T06).

Frozen Pydantic v2, ``extra="forbid"`` on every input boundary (fail-fast on
unexpected fields). The OpenAPI spec FastAPI derives from these is the contract
the web app's TypeScript client (spec 09) is generated from — keep them clean.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — a runtime Pydantic field type
from typing import Literal

from persona.schedules import RecurrencePattern  # noqa: TC001 — a runtime Pydantic field type
from pydantic import BaseModel, ConfigDict, Field, JsonValue

__all__ = [
    "ApprovalDecisionRequest",
    "AuthorPersonaRequest",
    "BudgetExtendRequest",
    "ChannelContext",
    "CreateConversationRequest",
    "ScheduleCreateRequest",
    "ScheduleRescheduleRequest",
    "CreateMCPServerRequest",
    "CreatePersonaRequest",
    "ImageRef",
    "MemoryCorrectionRequest",
    "PostMessageRequest",
    "RefinePersonaRequest",
    "RespondToRunRequest",
    "StartRunRequest",
    "UpdateMCPServerRequest",
    "UpdatePersonaRequest",
    "UpdateProfileRequest",
]

#: BYO-MCP auth methods (spec 30, D-30-3; Spec R8 adds ``oauth``). ``none`` and
#: ``bearer`` are user-supplied; ``oauth`` (R8) obtains the token via the OAuth dance
#: (no credential supplied at create — an ``oauth_provider`` is required instead).
#: ``header`` is reserved in the DB column for a later increment.
MCPAuthMethod = Literal["none", "bearer", "oauth"]


class _Input(BaseModel):
    """Base for request bodies: frozen, reject unknown fields (fail-fast)."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ChannelContext(_Input):
    """Opaque connector context passed through the chat endpoint (D-08-3).

    The API stores this on the message row and never interprets it — ``platform``
    is a free-form string, NEVER an enum the API branches on. All connector logic
    lives in the future spec-12 connectors. Null/absent is the web-UI case.
    """

    platform: str
    platform_user_id: str | None = None
    platform_chat_id: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)


class MemoryCorrectionRequest(_Input):
    """Correct a Memory node's content (Spec K5, K5-D-7 / K5-D-5).

    The user's edit to what a node says — the highest-quality write the graph gets.
    Content-only (per K5-D-7): it flows through K0's update path (re-embed, re-index,
    semantic links re-evaluated) and records provenance as ``user``-edited.
    """

    content: str = Field(min_length=1, max_length=8000)


class CreatePersonaRequest(_Input):
    """Create a persona from a YAML document (validated against the v1.0 schema).

    ``avatar_url`` is an optional presentation field (not part of the YAML
    schema) — the persona-list / chat-header visual identity.
    """

    yaml: str
    avatar_url: str | None = None


class UpdatePersonaRequest(_Input):
    """Replace a persona's YAML (re-validated against the v1.0 schema)."""

    yaml: str
    avatar_url: str | None = None


class SetConsentRequest(_Input):
    """Set a persona's auto-dispatch consent (spec 21 T09, D-21-7/2).

    ``granted``: ``True`` = grant (auto-dispatch), ``False`` = decline (stable,
    no re-prompt), ``None`` = revoke back to "ask" (the settings-toggle OFF
    path, which re-arms the prompt on the next autonomous dispatch).
    """

    granted: bool | None = None


class SetSkillConsentRequest(_Input):
    """Record consent for a community/third-party speciality (Spec S3, S3-D-2).

    ``granted``: ``True`` = grant (the skill may inject at its current body hash),
    ``False`` = revoke. That is the ONLY field a client may send — the
    ``content_hash`` consent binds to and the trust ``tier`` are **server-derived**
    from the catalog on every request, never accepted from the client (the
    forge-prevention invariant, S3-D-2). ``extra="forbid"`` (inherited from
    ``_Input``) rejects a client that tries to supply either → 422.
    """

    granted: bool


class AuthorPersonaRequest(_Input):
    """LLM-assisted authoring from a natural-language description (§5.1, §6.3)."""

    description: str = Field(min_length=1, max_length=4000)


class GrantToolRequest(_Input):
    """Enable a tool on a persona's allow-list via runtime consent (spec 26 T11).

    Sent when the user accepts a runtime tool-gap offer. ``turn_index`` is the
    conversation turn the offer came from (recorded in the persona_self audit).
    """

    tool_name: str = Field(min_length=1, max_length=128)
    turn_index: int | None = None


class RefinePersonaRequest(_Input):
    """Refine a draft persona by answering a clarifying question (spec 10, §4 / D-10-2).

    Stateless: ``round`` is the count of refinements already applied (the UI owns
    the counter); the server rejects ``round > 3`` as the backstop on the
    3-round cap (D-10-5).
    """

    current_yaml: str = Field(min_length=1)
    question: str = Field(min_length=1)
    answer: str = Field(min_length=1)
    round: int = Field(default=0, ge=0)


class CreateConversationRequest(_Input):
    """Start a new conversation against a persona.

    ``origin`` is the conversation's immutable birth-marker (Spec V9, V9-D-3):
    ``'chat'`` (the default — every text-path conversation) or ``'call'`` (the
    web sets this when it creates a conversation to host a voice call,
    V9-D-X-marker-writer-web). It is the ONLY seam between chat and voice; the
    closed ``Literal`` keeps the vocabulary shut at the request boundary
    (``extra="forbid"`` means the field must be declared, not silently passed).
    """

    title: str = ""
    origin: Literal["chat", "call"] = "chat"


# Defined as a sibling Pydantic v2 frozen model on the API request surface
# (NOT imported from ``persona_api.services.image_service.ImageRef``): the
# image-service dataclass is the internal upload-return type; this Pydantic
# model is the external request-body shape — matching the rest of the
# request-model conventions in this file (frozen, ``extra="forbid"``,
# OpenAPI-derivable).
class ImageRef(_Input):
    """Image reference carried on a chat message (spec 13, D-13-X-now option c).

    Refers to a previously-uploaded image in the persona's workspace (Spec 03).
    Image bytes live exactly once in the workspace; the chat body and the
    persisted ``messages`` row carry only ``workspace_path`` + ``media_type``
    so storage scales with reference count, not with image bytes.

    Attributes:
        workspace_path: Workspace-relative path returned by the uploads route
            (``uploads/<ref>.<ext>``). Resolved against
            ``workspace_root/owner_id/persona_id`` at backend send time.
        media_type: One of the four supported image MIME types per D-13-3:
            ``image/png``, ``image/jpeg``, ``image/webp``, ``image/gif``.
            Any other value is rejected at validation time.
    """

    workspace_path: str = Field(min_length=1)
    media_type: Literal["image/png", "image/jpeg", "image/webp", "image/gif"]


class PostMessageRequest(_Input):
    """Send a user message; the response streams over SSE (§5.2).

    ``channel`` is the optional connector passthrough (D-08-3) — null for the
    web UI. The runtime ignores it in v0.1; the API just stores it on the
    message row and echoes ``format_hints`` on the ``done`` event.

    ``images`` is the optional spec-13 multimodal extension (D-13-X-now option
    c, D-13-5): up to 4 :class:`ImageRef` per message. ``None`` (the default)
    keeps the text-only path byte-for-byte unchanged. An empty list is
    equivalent to ``None`` semantically but rejected as a validation error so
    callers don't accidentally send ``images=[]`` and skip the cap check; pass
    ``None`` or omit the field.

    The cap is enforced via :class:`Field`'s built-in ``min_length`` /
    ``max_length`` (D-13-5) so the failure surfaces as a structured
    ``too_long`` / ``too_short`` Pydantic v2 error — JSON-serialisable through
    the API's ``_request_422`` handler in :mod:`persona_api.errors` (a custom
    ``field_validator`` would attach a raw :class:`ValueError` to ``ctx`` and
    break the response body's ``json.dumps``).
    """

    content: str = Field(min_length=1)
    channel: ChannelContext | None = None
    images: list[ImageRef] | None = Field(default=None, min_length=1, max_length=4)


class StartRunRequest(_Input):
    """Start an agentic run for a task (§5.3)."""

    task: str = Field(min_length=1)


class RespondToRunRequest(_Input):
    """Answer an ask-user question raised by a running agentic loop (§5.3)."""

    answer: str


class CreateMCPServerRequest(_Input):
    """Add a bring-your-own MCP server (spec 30, D-30-3/4).

    ``url`` is SSRF-validated (https-only, public target) at the route AND on
    every live connect. ``credential`` (a bearer token for ``auth_method =
    "bearer"``) is encrypted at rest (T07) and NEVER returned or logged; it is
    required when ``auth_method`` is not ``"none"``.
    """

    name: str = Field(min_length=1, max_length=128)
    url: str = Field(min_length=1, max_length=2048)
    auth_method: MCPAuthMethod = "none"
    credential: str | None = Field(default=None, max_length=4096, repr=False)
    # Spec R8: required when ``auth_method = "oauth"`` — the provider-registry key
    # (e.g. ``github``). No credential is supplied for oauth; the token is obtained
    # via ``POST /mcp-servers/{id}/oauth/authorize`` then the callback.
    oauth_provider: str | None = Field(default=None, max_length=64)


class MCPOAuthAuthorizeRequest(_Input):
    """Start an OAuth flow for a BYO MCP server (Spec R8, T4).

    ``redirect_after`` is an OPTIONAL app-relative path the web callback returns the
    user to once connected — it is stored SERVER-SIDE against the state (never encoded
    in the OAuth ``state`` value) and is never an external redirect target.
    """

    redirect_after: str | None = Field(default=None, max_length=512)


class MCPOAuthCallbackRequest(_Input):
    """Complete an OAuth flow (Spec R8, T5): the web callback relays ``state`` + ``code``.

    Sent by the authenticated web callback page (which received the provider redirect).
    ``state`` is the opaque CSRF token minted at authorize; ``code`` the provider's
    one-time authorization code. Both are consumed server-side and never returned.
    """

    state: str = Field(min_length=1, max_length=512, repr=False)
    code: str = Field(min_length=1, max_length=4096, repr=False)


class AdoptCatalogAppRequest(_Input):
    """Self-adopt a catalog app for a persona (Spec N4, the B2-③ setup-form target).

    The connection ``url`` and ``auth_method`` are derived from the catalog entry
    server-side (N4-D-10 — the catalog is the trust anchor for *where* it connects); the
    caller supplies ONLY ``credential`` (when the app declares a secret). ``credential`` is
    ``repr=False`` (redacted in logs), encrypted at rest via the store, and NEVER returned.
    """

    catalog_name: str = Field(min_length=1, max_length=128)
    credential: str | None = Field(default=None, max_length=4096, repr=False)


class UpdateMCPServerRequest(_Input):
    """Patch a BYO MCP server (spec 30). All fields optional; omitted = unchanged.

    Setting ``credential`` replaces the stored secret (re-encrypted); to clear a
    credential, set ``auth_method = "none"``. ``enabled`` toggles the server
    without deleting it.
    """

    name: str | None = Field(default=None, min_length=1, max_length=128)
    url: str | None = Field(default=None, min_length=1, max_length=2048)
    auth_method: MCPAuthMethod | None = None
    credential: str | None = Field(default=None, max_length=4096, repr=False)
    enabled: bool | None = None


class UpdateProfileRequest(_Input):
    """Set the caller's optional name + timezone + model preference (Spec K6/A8/M1).

    PATCH semantics. All fields optional; **omitted = unchanged**, explicit ``null`` =
    **clear** (distinguished server-side via ``model_dump(exclude_unset=True)``).
    ``max_length`` fails fast at the boundary on egregious input; the service then
    strips control characters and treats whitespace-only as unset (names,
    :func:`persona_api.services.user_service.normalize_name`). ``timezone`` (Spec A8,
    A8-D-9) is an IANA zone name whose validity is checked in the route handler
    (:func:`persona.timezone.validate_timezone` → 422) — the ``max_length`` here is
    only a cheap egregious-input guard, not the IANA check (a custom ``field_validator``
    would break the 422 body's ``json.dumps``, cf. ``SendMessageRequest``). ``null``
    clears it → schedule computation falls back to ``PERSONA_DEFAULT_TIMEZONE``.
    Nothing is required — an empty PATCH is a valid no-op read.
    """

    first_name: str | None = Field(default=None, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    timezone: str | None = Field(default=None, max_length=64)
    #: Quiet hours (Spec A8, A8-D-6): local minutes-of-day [start, end). Send both to set,
    #: both ``null`` to clear (off-until-set). Coherence (both-or-neither, start != end) is
    #: validated in the route handler (a 422), like the timezone IANA check.
    quiet_hours_start: int | None = Field(default=None, ge=0, le=1439)
    quiet_hours_end: int | None = Field(default=None, ge=0, le=1439)
    #: Sticky last-choice model preference (Spec M1, M1-T6): the OpenRouter model id
    #: the caller picked in the web model picker. ``null`` clears it → the runtime
    #: loop falls back to the tier-resolved default. Catalog validity (does this id
    #: exist / is it chat-capable) is the WEB picker's concern (M1-T5) — the API only
    #: stores the preference, it never calls the catalog here. ``min_length=1`` +
    #: ``pattern=r"\\S"`` reject blank/whitespace-only as a structured 422 via Field
    #: constraints alone (no custom ``field_validator`` — same rationale as the
    #: ``timezone`` note above: a raised ``ValueError`` risks the 422 body's
    #: ``json.dumps``).
    preferred_model: str | None = Field(default=None, min_length=1, max_length=256, pattern=r"\S")


class ScheduleRescheduleRequest(_Input):
    """A calendar-initiated reschedule (Spec A8, T9 — the twin of the chat verb).

    **Picker-state in — NO raw RRULE from the client** (bar 1): the web sends a structured
    :class:`~persona.schedules.RecurrencePattern` (or a one-time instant) and the SERVER maps it to
    the rule, so there is no client-side recurrence math. Exactly one of ``pattern`` /
    ``one_time_at`` (XOR, checked in the route). Applied through the SAME CAS door as chat.
    """

    pattern: RecurrencePattern | None = None
    one_time_at: datetime | None = None
    timezone: str = Field(min_length=1, max_length=64)


class ScheduleCreateRequest(_Input):
    """A user-initiated schedule create (Spec A10, A10-D-1 — the third verb on A8's door).

    The A8 reschedule envelope + the create-only fields. **Picker-state in — NO raw RRULE
    from the client** (the A8 bar): exactly one of ``pattern`` / ``one_time_at`` (XOR,
    checked in the route); the SERVER maps pattern → rule. The user is the originator; the
    named persona is the executor who delivers each fire. ``idempotency_key`` is minted by
    the client once per create dialog (A10-D-6): retries/double-clicks converge on one
    task+schedule, while two deliberate submits (two dialog-opens) stay distinct.
    """

    pattern: RecurrencePattern | None = None
    one_time_at: datetime | None = None
    timezone: str = Field(min_length=1, max_length=64)
    persona_id: str = Field(min_length=1, max_length=128)
    subject: str = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(min_length=8, max_length=128)
    # Opt into the coalesced fire bell (default True — this door is always a user reminder;
    # the dialog's "Notify me in the bell" checkbox is checked by default and can be unset).
    notify_on_fire: bool = True


class BudgetExtendRequest(_Input):
    """Raise a budget-paused task's cap by ``amount_micros`` (Spec A6, B2) — bounded server-side."""

    amount_micros: int = Field(gt=0)


class InitiativeDialRequest(_Input):
    """Set a persona's initiative restraint level (Spec A6, B4 — the dial switch).

    ``off`` silences the scan, ``propose_only`` converts acts to proposals, ``act_within_envelope``
    lets all-safe plans execute. The level persists regardless of the platform initiative flag; the
    route reflects whether initiative is globally enabled so the UX is honest about when it acts.
    """

    dial: Literal["off", "propose_only", "act_within_envelope"]


class ApprovalDecisionRequest(_Input):
    """An inbox approval decision (Spec A6, criterion 5) — the structured twin of a chat reply.

    ``edited_arguments`` is required for (and only meaningful on) a ``modify`` — the inbox's
    modify-inline edit; the resolver's floor decides materiality (a material edit re-confirms). A
    material change presented as instantly-applied would be a lie, so the client reflects the
    resolver's outcome, never assumes. ``note`` is optional free text, recorded as the decision's
    durable verbatim reply.
    """

    decision: Literal["approve", "deny", "modify"]
    edited_arguments: dict[str, JsonValue] | None = None
    note: str = Field(default="", max_length=2000)
