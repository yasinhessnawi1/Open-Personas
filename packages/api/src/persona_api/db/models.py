"""Canonical SQLAlchemy Core schema for the hosted service (spec 07, T03).

This module is the single source of truth for the production schema's *shape*.
The Alembic ``001_initial`` migration (T04) creates these tables (plus RLS, T06)
via explicit SQL; this ``MetaData`` is wired as Alembic's ``target_metadata`` so
autogenerate stays sane. The ``persona-core`` Postgres transport
(``stores/postgres.py``) defines its own minimal view of ``memory_chunks`` —
core cannot import this api-only module — and a contract test (T07) asserts the
two agree.

Decisions in force here:

- **D-07-2:** SQLAlchemy Core (no ORM). Typed ``Table`` definitions, queried
  with parameterised Core expressions.
- **D-07-4:** ``memory_chunks`` *promotes* the versioning/provenance fields
  (``logical_id``/``version``/``superseded_by`` + ``content_hash`` + the
  ``ChunkProvenance`` fields) to indexed columns; user metadata lives in the
  ``metadata`` JSONB column; identity chunks store NULL provenance. ``decay_t0``
  is dropped — decay anchors on ``created_at`` at query time (D-01-4).
- **D-07-4 / S07-3:** HNSW index with ``vector_cosine_ops`` on ``embedding``;
  a partial index on the current-heads predicate (``superseded_by IS NULL``).
- **Embedding dim:** ``vector(384)`` (bge-small-en-v1.5). v0.1 single-embedder.
"""

from __future__ import annotations

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Computed,
    Date,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, REAL, TSVECTOR


def _json() -> JSON:
    """A dialect-neutral JSON column type (Spec 33, D-33-X-json-variant).

    Emits ``JSONB`` on PostgreSQL (cloud — byte-identical to the pre-Spec-33
    DDL, so no migration) and the generic ``JSON`` type on SQLite (community).
    A fresh instance per column keeps each ``Table`` definition independent.
    """
    return JSON().with_variant(JSONB(), "postgresql")


__all__ = [
    "EMBEDDING_DIM",
    "STORE_KINDS",
    "audit_log",
    "conversations",
    "credit_transactions",
    "credits",
    "day_spend",
    "graph_edges",
    "graph_entities",
    "graph_node_entities",
    "graph_nodes",
    "inflight_ops",
    "jobs",
    "jobs_archive",
    "memory_chunks",
    "messages",
    "metadata",
    "persona_mcp_assignments",
    "personas",
    "rate_limit_buckets",
    "request_telemetry",
    "runs",
    "schedules",
    "store_audit_events",
    "tool_audit_events",
    "turn_logs",
    "user_mcp_servers",
    "users",
]

# bge-small-en-v1.5 (architecture §4.3). The column is hard-coded to this dim;
# a chunk whose embedding length differs is rejected at write (steer #7).
EMBEDDING_DIM = 384

STORE_KINDS = ("identity", "self_facts", "worldview", "episodic")

metadata = MetaData()

_uuid_pk = text("gen_random_uuid()::text")


users = Table(
    "users",
    metadata,
    Column("id", Text, primary_key=True),
    Column("email", Text, unique=True, nullable=False),
    # Optional, first-class user identity (Spec K6, K6-D-1). Captured through our
    # OWN app step (``PATCH /v1/me/profile``) into our DB — Clerk stays auth-only
    # (username off; we do not use its required-name fields). Both nullable: a
    # nameless / pre-K6 account is fully valid everywhere (null-safe), and the
    # name is never a hard requirement (signup, persona, graph all tolerate NULL).
    # Split-home (cf. avatar_source / migrations 020 / 023 / 025): declared here so
    # ``001_initial``'s ``metadata.create_all`` builds them on a fresh DB, and
    # migration 028's guarded ``ADD COLUMN IF NOT EXISTS`` adds them to a
    # previously-deployed Postgres. TEXT (not CHECK/ENUM) — the value vocabulary is
    # free-form; validation (length cap, control-char strip) is at the app layer
    # (K6-D-8). ``users`` is NOT RLS-scoped (globally readable; child tables carry
    # the tenant RLS), so a new nullable column needs no policy change.
    Column("first_name", Text),
    Column("last_name", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

personas = Table(
    "personas",
    metadata,
    Column("id", Text, primary_key=True, server_default=_uuid_pk),
    Column("owner_id", Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("yaml", Text, nullable=False),
    Column("schema_version", Text, nullable=False, server_default=text("'1.0'")),
    # Visual identity for the persona list / chat header (nullable: user-uploaded
    # or auto-generated-from-initials by the frontend). Added by migration 003,
    # pre-spec-09 patch. Not part of the persona YAML schema — a presentation
    # field owned by the API row.
    Column("avatar_url", Text),
    # Synthetic-media provenance for the avatar (Spec R3, R3-D-2 / EU AI Act Art. 50).
    # ``'generated'`` = the avatar bytes were produced by the system's image-gen
    # path (the inline create-hook or the async avatar job); ``'uploaded'`` = a user
    # supplied the bytes via the upload-to-change PATCH; ``NULL`` = unknown (every
    # row that predates this column — the explicit, honest backfill state per
    # R3-D-5: pre-existing avatars cannot be reliably distinguished post-hoc, so
    # they read unknown rather than being mislabelled). The Art. 50 "AI-generated"
    # disclosure is *derived* from this stored signal (``avatar_source == 'generated'``),
    # never guessed. Set in the SAME write as ``avatar_url`` so it is unforgeable
    # (R3-D-3 — no window where the url is set but provenance is NULL). Added by
    # migration 025; nullable TEXT (not an SQL ENUM/CHECK — string keeps the
    # community SQLite edition byte-identical to Postgres; app-layer is the gate).
    # Like ``avatar_url`` this is an API-row presentation field, NOT part of the
    # persona YAML schema, so it does not move ``schema_version``.
    Column("avatar_source", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    # Spec 21 T09 (D-21-7): tri-state auto-dispatch consent. NULL = never asked /
    # revoked-to-ask, TRUE = granted, FALSE = explicitly declined (stable, never
    # auto-re-prompts — D-21-17). Added by migration 008; nullable so existing
    # rows are unaffected. consent_updated_at stamps the last transition (each
    # transition also emits an AuditEvent at the route).
    Column("consent_to_auto_dispatch", Boolean),
    Column("consent_updated_at", DateTime(timezone=True)),
    # Lets conversations/runs reference (persona_id, owner_id) as a composite FK
    # so a row can never attach to another tenant's persona (defence-in-depth
    # beyond RLS — see spec-07 closeout / security review finding 1).
    UniqueConstraint("id", "owner_id", name="uq_personas_id_owner"),
    Index("idx_personas_owner", "owner_id"),
)

conversations = Table(
    "conversations",
    metadata,
    Column("id", Text, primary_key=True, server_default=_uuid_pk),
    Column("owner_id", Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("persona_id", Text, nullable=False),
    Column("title", Text, nullable=False, server_default=text("''")),
    # Spec V9 D-V9-3 / migration 020: the conversation's IMMUTABLE birth-origin —
    # 'chat' (text-born) or 'call' (voice-born). Set ONCE at creation by whoever
    # starts it; it is the ONLY seam between chat and voice (the chat list excludes
    # 'call'; the Calls surface reads the call-record). DEFAULT 'chat' backfills
    # every pre-V9 row (all chat-born). NOT what a conversation currently CONTAINS
    # — a chat later called stays 'chat' (mixed-case correctness).
    Column("origin", Text, nullable=False, server_default=text("'chat'")),
    Column("compacted_summary", Text, nullable=False, server_default=text("''")),
    Column("compacted_up_to", Integer, nullable=False, server_default=text("0")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    # Composite FK: the persona must belong to the SAME owner (the DB rejects a
    # conversation attached to another tenant's persona — security finding 1).
    ForeignKeyConstraint(
        ["persona_id", "owner_id"],
        ["personas.id", "personas.owner_id"],
        ondelete="CASCADE",
        name="fk_conversations_persona_owner",
    ),
    CheckConstraint("origin IN ('chat', 'call')", name="conversations_origin_check"),
    Index("idx_conversations_owner", "owner_id"),
    Index("idx_conversations_persona", "persona_id"),
)

messages = Table(
    "messages",
    metadata,
    Column("id", Text, primary_key=True, server_default=_uuid_pk),
    Column(
        "conversation_id",
        Text,
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("role", Text, nullable=False),
    Column("content", Text, nullable=False),
    Column("tool_calls", _json()),
    # Connector passthrough (spec 08, D-08-3, migration 002). Nullable: the web
    # UI sends no channel. The API stores it opaquely and never branches on
    # `platform` — all connector logic is the future spec 12's.
    Column("channel", _json()),
    # Spec 13 D-13-X-now option (c) / migration 004: per-message image refs as
    # JSONB. Each entry is ``{"workspace_path": str, "media_type": str}``. The
    # row holds REFERENCES only — image bytes live exactly once under the Spec
    # 03 workspace (D-13-4). Nullable: text-only messages and every assistant
    # message persist with images = NULL (byte-for-byte unchanged for the
    # text-only path).
    Column("images", _json()),
    # Spec 35 D-35-2 / migration 010: the routing tier (small/mid/frontier) the
    # router chose for this assistant turn, persisted so the per-message tier
    # chip survives a page reload (the live `done` event only covers the
    # just-streamed turn). Nullable: user/system/tool rows and every message
    # written before this migration carry NULL → the chip simply does not render
    # (clean degrade, D-35-2), never a wrong tier.
    Column("tier_used", Text),
    # Spec C0 D-C0-X-discriminator (DB half) / migration 013: an originated
    # (persona-initiated) message — one the persona produced with no preceding
    # user turn. role stays 'assistant' (orthogonal axis — the role CHECK is
    # untouched); this boolean is the persisted who-initiated discriminator and
    # the queryable source of truth. The persona-api boundary maps it to/from the
    # in-core ``metadata["originated"]`` marker. NOT NULL DEFAULT false: every
    # historical / solicited row reads false (correct — they were all solicited).
    Column("originated", Boolean, nullable=False, server_default=text("false")),
    # Spec P1 D-P1-checkpoint / migration `018_add_message_streaming_state`: the
    # detached-turn streaming lifecycle for THIS assistant row. A chat turn now
    # runs in a background task and is checkpointed AS it streams; this column
    # is the persisted lifecycle of the in-progress assistant message:
    # ``running`` while the turn streams, then a terminal value. **NULL = a
    # legacy / non-streamed row** (every historical message + every message
    # written by a non-P1 path) — it renders as a plain final message (clean
    # degrade, the ``tier_used``/``originated`` nullable-additive precedent).
    # **DB-persistence state ONLY — never a ``ConversationMessage`` model field**
    # (the C0 lesson: a top-level model field would break the Spec-13
    # byte-for-byte dump corpus).
    Column("streaming_status", Text),
    # Spec P1 D-P1-checkpoint-scope: the partial event-log (text deltas + tool
    # events) accumulated as the turn streams — same shape as ``runs.steps`` —
    # so a reattach-after-gap reconstructs the tool/text interleave, not just the
    # final text. NULL for legacy / text-only / non-streamed rows. DB-only.
    Column("stream_events", _json()),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("role IN ('system', 'user', 'assistant', 'tool')", name="messages_role_check"),
    # Spec P1 D-P1-checkpoint: the allowed streaming lifecycle values (NULL =
    # legacy/non-streamed). ``running`` is the in-flight state; the rest are
    # terminal (``interrupted`` = reconciled by the restart sweep, D-P1-restart-sweep).
    CheckConstraint(
        "streaming_status IS NULL OR streaming_status IN "
        "('running', 'complete', 'cancelled', 'interrupted', 'error')",
        name="messages_streaming_status_check",
    ),
    Index("idx_messages_conversation", "conversation_id"),
    Index("idx_messages_created", "conversation_id", "created_at"),
    # Spec P1 D-P1-one-active-turn: the DB-level guarantee of EXACTLY ONE active
    # (streaming) turn per conversation — a partial unique index over the
    # in-flight rows. Backstops the in-process ``ChatTurnRegistry`` check against
    # a race. Declared for both dialects (Postgres cloud + SQLite community).
    Index(
        "uq_messages_one_streaming_per_conversation",
        "conversation_id",
        unique=True,
        postgresql_where=text("streaming_status = 'running'"),
        sqlite_where=text("streaming_status = 'running'"),
    ),
)

runs = Table(
    "runs",
    metadata,
    Column("id", Text, primary_key=True, server_default=_uuid_pk),
    Column("owner_id", Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("persona_id", Text, nullable=False),
    Column("task", Text, nullable=False),
    Column("status", Text, nullable=False, server_default=text("'running'")),
    Column("steps", _json(), nullable=False, server_default=text("'[]'")),
    Column("output", Text),
    Column("error", Text),
    Column("started_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("finished_at", DateTime(timezone=True)),
    CheckConstraint(
        "status IN ('running', 'awaiting_user', 'completed', 'cancelled', "
        "'max_steps_reached', 'error')",
        name="runs_status_check",
    ),
    # Composite FK: a run's persona must belong to the same owner (finding 1).
    ForeignKeyConstraint(
        ["persona_id", "owner_id"],
        ["personas.id", "personas.owner_id"],
        ondelete="CASCADE",
        name="fk_runs_persona_owner",
    ),
    Index("idx_runs_owner", "owner_id"),
)

# Spec A0 (durable execution) — the Postgres-backed job queue.
#
# ``jobs`` is the hot queue table: a worker claims with ``SELECT … FOR UPDATE
# SKIP LOCKED`` (the partial ``idx_jobs_claim`` index serves the claim predicate),
# writes a lease, and heartbeats while running; an expired lease returns the job
# to claimable (crash-resume by construction). Hygiene against the known
# Postgres-as-queue slow-death (D-A0-4): ``fillfactor=80`` so status updates stay
# HOT (no index churn), aggressive per-table autovacuum, and a small partial-index
# set tuned to the only two hot predicates (claim + lease-reclaim). ``state``
# mirrors the persona-core ``JobState`` machine; ``idempotency_key`` is UNIQUE so a
# duplicate enqueue is a no-op (``ON CONFLICT (idempotency_key) DO NOTHING``).
# Owner-scoped + RLS like every tenant table — the worker runs as the
# ``persona_app`` non-superuser role (RLS policy created in migration 011).
jobs = Table(
    "jobs",
    metadata,
    Column("id", Text, primary_key=True, server_default=_uuid_pk),
    Column("type", Text, nullable=False),
    Column("owner_id", Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("payload", _json(), nullable=False, server_default=text("'{}'")),
    Column("idempotency_key", Text, nullable=False),
    Column("state", Text, nullable=False, server_default=text("'queued'")),
    Column("priority", Integer, nullable=False, server_default=text("0")),
    Column("attempt", Integer, nullable=False, server_default=text("0")),
    Column("max_attempts", Integer, nullable=False, server_default=text("5")),
    Column("scheduled_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("lease_expires_at", DateTime(timezone=True)),
    Column("locked_by", Text),
    Column("last_error", Text),
    CheckConstraint(
        "state IN ('queued', 'claimed', 'running', 'succeeded', 'failed', 'dead')",
        name="jobs_state_check",
    ),
    # Duplicate enqueues dedup on this key (operation+intent scoped — D-A0-X-...).
    # Scoped per OWNER, not globally: a global key namespace would let one tenant
    # pre-register another tenant's key and silently suppress their enqueue
    # (cross-tenant DoS — security review T4). Owner-scoping also matches the RLS
    # WITH CHECK semantics: a key means "this operation for this owner".
    UniqueConstraint("owner_id", "idempotency_key", name="uq_jobs_owner_idempotency_key"),
    # The claim query: WHERE state='queued' AND scheduled_at<=now()
    #                  ORDER BY priority DESC, scheduled_at  (FOR UPDATE SKIP LOCKED).
    Index(
        "idx_jobs_claim",
        text("priority DESC"),
        text("scheduled_at"),
        postgresql_where=text("state = 'queued'"),
    ),
    # The rescuer sweep: reclaim leases that lapsed under a dead/draining worker.
    Index(
        "idx_jobs_lease_expiry",
        "lease_expires_at",
        postgresql_where=text("state IN ('claimed', 'running')"),
    ),
    # RLS predicate filters on owner_id.
    Index("idx_jobs_owner", "owner_id"),
    # Claim-time fairness (T7): the per-user + global in-flight counts scan only
    # claimed/running rows; this partial index serves both (by owner, and total).
    Index(
        "idx_jobs_inflight_by_owner",
        "owner_id",
        postgresql_where=text("state IN ('claimed', 'running')"),
    ),
)
# D-A0-4 hygiene (HOT updates via fillfactor + aggressive per-table autovacuum)
# is applied as storage reloptions in migration 011 — ``ALTER TABLE jobs SET
# (...)`` — not here, since SQLAlchemy's ``Table`` has no portable reloptions
# kwarg. Tunable via ALTER once the soak test (A0-R-3) measures the real churn.

# ``jobs_archive`` is the cold table terminal jobs age out into (the cleaner sweep,
# D-A0-4): keeps the hot table's working set tiny while A3/A6 still read job
# history. Same shape as ``jobs`` plus ``archived_at``; no claim/lease indexes
# (never claimed) and no idempotency UNIQUE (the same key may recur across time —
# the live dedup lives on the hot table). Terminal-only by CHECK. Owner-scoped +
# RLS (it holds tenant data; migration 011 creates the policy).
jobs_archive = Table(
    "jobs_archive",
    metadata,
    Column("id", Text, primary_key=True),
    Column("type", Text, nullable=False),
    Column("owner_id", Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("payload", _json(), nullable=False, server_default=text("'{}'")),
    Column("idempotency_key", Text, nullable=False),
    Column("state", Text, nullable=False),
    Column("priority", Integer, nullable=False, server_default=text("0")),
    Column("attempt", Integer, nullable=False, server_default=text("0")),
    Column("max_attempts", Integer, nullable=False, server_default=text("5")),
    Column("scheduled_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("lease_expires_at", DateTime(timezone=True)),
    Column("locked_by", Text),
    Column("last_error", Text),
    Column("archived_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(
        "state IN ('succeeded', 'failed', 'dead')",
        name="jobs_archive_state_check",
    ),
    Index("idx_jobs_archive_owner", "owner_id"),
    # Retention sweeps delete by age.
    Index("idx_jobs_archive_archived_at", "archived_at"),
)

# ``schedules`` is A1's durable clock: a recurring RRULE-class rule OR a one-time
# future instant, with the user's captured IANA timezone on the row. The
# single-leader tick (in the worker) claims due rows and materialises each fire
# into an A0 ``jobs`` row keyed by ``sched:{id}:{fire_time}`` (riding A0's
# effectively-once). Unlike ``jobs`` this is a LOW-VOLUME entity table (one row
# per schedule, updated once per fire) — no queue-hygiene reloptions; default
# autovacuum is right. Owner-scoped + RLS like every tenant table (the worker's
# dispatch engine reads/updates it cross-tenant; the RLS engine owner-scopes
# creation). The recurrence/one-time XOR is enforced at the DB, mirroring the
# ``persona.schedules.Schedule`` validator. RLS policy created in migration
# ``014_schedules``.
schedules = Table(
    "schedules",
    metadata,
    Column("id", Text, primary_key=True, server_default=_uuid_pk),
    Column("owner_id", Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("timezone", Text, nullable=False),
    # The RFC-5545 RRULE string (NULL for a one-time schedule).
    Column("recurrence", Text),
    # The one-time future instant (NULL for a recurring schedule).
    Column("one_time_at", DateTime(timezone=True)),
    Column("target_job_type", Text, nullable=False),
    Column("payload_template", _json(), nullable=False, server_default=text("'{}'")),
    Column("enabled", Boolean, nullable=False, server_default=text("true")),
    Column("paused", Boolean, nullable=False, server_default=text("false")),
    Column(
        "missed_fire_policy",
        Text,
        nullable=False,
        server_default=text("'fire-late-once'"),
    ),
    Column("grace_seconds", Integer),
    Column("last_fire_at", DateTime(timezone=True)),
    Column("next_fire_at", DateTime(timezone=True)),
    Column("fire_count", Integer, nullable=False, server_default=text("0")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    # The recurrence/one-time XOR — exactly one of the two is set. Mirrors the
    # Schedule entity's model validator so the invariant holds even for a direct
    # SQL write (defence in depth).
    CheckConstraint(
        "(recurrence IS NOT NULL) <> (one_time_at IS NOT NULL)",
        name="schedules_recurrence_xor_one_time",
    ),
    CheckConstraint(
        "missed_fire_policy IN ('fire-late-once', 'skip-and-note')",
        name="schedules_missed_fire_policy_check",
    ),
    CheckConstraint(
        "grace_seconds IS NULL OR grace_seconds >= 0",
        name="schedules_grace_seconds_check",
    ),
    CheckConstraint("fire_count >= 0", name="schedules_fire_count_check"),
    # The tick's due-claim: WHERE enabled AND NOT paused AND next_fire_at IS NOT NULL
    #                       AND next_fire_at <= now()  ORDER BY next_fire_at.
    Index(
        "idx_schedules_due",
        "next_fire_at",
        postgresql_where=text("enabled AND NOT paused AND next_fire_at IS NOT NULL"),
    ),
    # The RLS predicate filters on owner_id.
    Index("idx_schedules_owner", "owner_id"),
)

# D-07-4: memory_chunks promotes provenance/versioning to indexed columns.
memory_chunks = Table(
    "memory_chunks",
    metadata,
    Column("id", Text, primary_key=True),
    # persona_id has discriminated semantics gated by ``kind`` (v0.1.1, migration 007):
    # rows with kind in {identity,self_facts,worldview,episodic} carry a personas.id;
    # rows with kind='document' carry a conversations.id (per migration 005's RLS aux
    # policy). PostgreSQL cannot express conditional FKs natively; the integrity
    # guarantee splits across the kind CHECK + RLS policy. Migration 007 drops the
    # FK to personas.id; here we omit the ForeignKey so create_all() matches the
    # post-migration shape.
    Column("persona_id", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("text", Text, nullable=False),
    Column("embedding", Vector(EMBEDDING_DIM), nullable=False),
    Column(
        "embedding_model",
        Text,
        nullable=False,
        server_default=text("'bge-small-en-v1.5'"),
    ),
    Column("content_hash", Text, nullable=False),
    # User-supplied PersonaChunk.metadata (string-valued) — NOT provenance.
    Column("metadata", _json(), nullable=False, server_default=text("'{}'")),
    # Promoted ChunkProvenance (NULL for identity chunks, which never version).
    Column("logical_id", Text),
    Column("version", Integer),
    Column("superseded_by", Text),
    Column("prov_source", Text),
    Column("written_at", DateTime(timezone=True)),
    Column("written_by", Text),
    Column("reason", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    # Spec 19 D-19-X-memory-chunks-kind-check-migration (chain entry 23):
    # ``'document'`` is the fifth accepted kind so the DocumentStore path
    # (migration 005 RLS aux policy) survives the CHECK. Migration 006 is the
    # migration of record for existing deployments; this canonical declaration
    # is the source of truth for fresh DBs via ``001_initial`` create_all.
    CheckConstraint(
        "kind IN ('identity', 'self_facts', 'worldview', 'episodic', 'document')",
        name="memory_chunks_kind_check",
    ),
    CheckConstraint(
        "prov_source IS NULL OR prov_source IN ('system', 'user', 'persona_self')",
        name="memory_chunks_prov_source_check",
    ),
    Index("idx_memory_persona_kind", "persona_id", "kind"),
    Index("idx_memory_persona_kind_logical", "persona_id", "kind", "logical_id"),
    # Partial index: the hot current-heads view (query/get_all default).
    Index(
        "idx_memory_current_heads",
        "persona_id",
        "kind",
        postgresql_where=text("superseded_by IS NULL"),
    ),
    # HNSW approximate-NN over cosine distance (S07-3).
    Index(
        "idx_memory_embedding",
        "embedding",
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    ),
)

turn_logs = Table(
    "turn_logs",
    metadata,
    Column("id", Text, primary_key=True, server_default=_uuid_pk),
    Column(
        "conversation_id",
        Text,
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("turn_index", Integer, nullable=False),
    Column("tier_used", Text, nullable=False),
    Column("model_name", Text, nullable=False),
    Column("provider", Text, nullable=False),
    Column("prompt_tokens", Integer, nullable=False),
    Column("completion_tokens", Integer, nullable=False),
    Column("latency_ms", Float, nullable=False),
    Column("cost_cents", Float, nullable=False, server_default=text("0")),
    Column("tool_calls", Integer, nullable=False, server_default=text("0")),
    Column("skill_used", Text),
    Column(
        "history_compacted",
        Boolean,
        nullable=False,
        server_default=text("FALSE"),
    ),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Index("idx_turn_logs_conversation", "conversation_id"),
    # Spec R7 (R7-D-1 discharge): supports the per-day soft-ramp aggregate — sum
    # ``cost_cents`` for a conversation over the current UTC day (sargable range on
    # ``created_at``). turn_logs is conversation-keyed (persona is reached via the
    # ``conversations`` join, itself owner-indexed), so this composite is the
    # load-bearing index for the day-window scan.
    Index("idx_turn_logs_conversation_created", "conversation_id", "created_at"),
)

rate_limit_buckets = Table(
    "rate_limit_buckets",
    metadata,
    Column("user_id", Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("endpoint", Text, nullable=False),
    Column("window_start", DateTime(timezone=True), nullable=False),
    Column("request_count", Integer, nullable=False, server_default=text("0")),
    PrimaryKeyConstraint("user_id", "endpoint", "window_start"),
)

credits = Table(  # noqa: A001 — schema table name (spec §5), not the stdlib `credits`
    "credits",
    metadata,
    Column("user_id", Text, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("balance", Integer, nullable=False, server_default=text("100000")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    # Spec R2 R2-D-3 (F-04): the durable DB-level floor for the money path. The
    # conditional decrement in ``persona.credits.service.deduct`` makes a negative
    # balance unreachable from the app; this CHECK enforces it regardless of any
    # future write path. Added on a deployed DB by migration 024 (after a one-shot
    # repair of any pre-fix negative rows); declared here so a fresh-DB
    # ``metadata.create_all`` builds it too (split-home discipline, cf. messages).
    CheckConstraint("balance >= 0", name="credits_balance_nonneg_check"),
)

credit_transactions = Table(
    "credit_transactions",
    metadata,
    Column("id", Text, primary_key=True, server_default=_uuid_pk),
    Column("user_id", Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("delta", Integer, nullable=False),
    Column("reason", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Index("idx_credit_tx_user", "user_id"),
)

audit_log = Table(
    "audit_log",
    metadata,
    Column("id", Text, primary_key=True, server_default=_uuid_pk),
    Column("user_id", Text, nullable=False),
    Column("action", Text, nullable=False),
    Column("target", Text, nullable=False),
    Column("metadata", _json(), nullable=False, server_default=text("'{}'")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Index("idx_audit_user", "user_id"),
    Index("idx_audit_created", "created_at"),
    # (target, action) — the A3 budget effective-cap read sums budget.extended rows per task
    # on the leg-boundary budget check; this keeps that lookup off a full audit_log scan (T10).
    Index("idx_audit_target_action", "target", "action"),
)

# Spec R7 (R7-D-1/2/3) — the per-user per-UTC-day spend counter, the primary
# denial-of-wallet guard. One integer row per (user_id, utc_day); the atomic
# conditional write in ``persona.credits.service.book_day_spend`` books spend
# only ``WHERE spent + :cost <= :cap`` (the R2 TOCTOU-safe family), so two
# concurrent near-cap requests can't both pass. RLS-scoped by ``user_id`` (its
# ENABLE/FORCE/policy live entirely in migration 026 — the 009/011/021 split-home
# template; deliberately NOT in ``db/rls._POLICIES`` since the table post-dates
# 001_initial). ``utc_day`` is a DATE keyed on the UTC calendar day (fixed-window
# reset — R7-D-2). The core-owned ``persona.credits`` mirror-view (T2) writes it
# on the caller's RLS engine; the api-side integration tests guard view↔DDL drift.
day_spend = Table(
    "day_spend",
    metadata,
    Column("user_id", Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("utc_day", Date, nullable=False),
    Column("spent", Integer, nullable=False, server_default=text("0")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    PrimaryKeyConstraint("user_id", "utc_day"),
    # A booked counter is never negative (refunds don't decrement the day counter —
    # a refund reverses the *balance*, not the day's spend commitment; R7-D-3). The
    # DB-level floor is belt-and-braces against any future write path.
    CheckConstraint("spent >= 0", name="day_spend_nonneg_check"),
)

# Spec R7 (R7-D-4) — the durable in-flight registry for LONG-running ops
# (chat SSE, agentic jobs) whose work spans no single short transaction, so the
# ``pg_try_advisory_xact_lock`` bounded-op primitive cannot hold across them. One
# row per admitted op; admission is count-filtered under a per-(user,op_class)
# advisory lock so exactly N are admitted (R7-D-4 HARD cap). The row is DELETEd on
# completion; a TTL-based stale-reclaim in ``persona.concurrency.admit_long_op``
# frees a slot a crashed op would otherwise leak. RLS-scoped by ``user_id`` (RLS
# lifecycle in migration 026, split-home template; not in ``db/rls._POLICIES``).
inflight_ops = Table(
    "inflight_ops",
    metadata,
    Column("id", Text, primary_key=True, server_default=_uuid_pk),
    Column("user_id", Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("op_class", Text, nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    # The count-filter admission (``WHERE count(inflight) < :max``) reads this index.
    Index("idx_inflight_by_owner", "user_id", "op_class"),
)

# Spec R5 (R5-D-1) — multi-worker-safe store-mutation audit. The JSONL
# ``persona.audit.JSONLAuditLogger`` assumes ONE writer (process-local
# ``threading.Lock``) and breaks the moment the API runs on N workers / N Fly
# machines (S08-4). ``PostgresAuditLogger`` writes here instead: PostgreSQL
# MVCC + WAL make concurrent INSERTs from N processes non-blocking, per-statement
# atomic, and durable — no lost / duplicated / interleaved-corrupted rows, no
# explicit locking for an append-only pattern. Columns mirror the frozen
# ``persona.audit.AuditEvent`` verbatim; ``list``/``dict`` fields → JSON(B).
# NON-RLS platform table (like ``audit_log`` / ``rate_limit_buckets``): written
# via a plain ``engine.begin()``, no owner GUC. This is FORENSIC store-mutation
# audit — distinct from ``audit_log`` (API-action forensics) and from
# ``tool_audit_events`` (below); do NOT conflate.
store_audit_events = Table(
    "store_audit_events",
    metadata,
    Column("id", Text, primary_key=True, server_default=_uuid_pk),
    Column("timestamp", DateTime(timezone=True), nullable=False),
    Column("persona_id", Text, nullable=False),
    Column("action", Text, nullable=False),
    Column("store", Text, nullable=False),
    Column("source", Text, nullable=False),
    Column("written_by", Text),
    Column("reason", Text),
    Column("chunk_ids", _json(), nullable=False, server_default=text("'[]'")),
    Column("logical_ids", _json(), nullable=False, server_default=text("'[]'")),
    Column("metadata", _json(), nullable=False, server_default=text("'{}'")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    # BRIN on the append-only, timestamp-ascending ``created_at``: hundreds of ×
    # smaller than BTREE with faster range scans for the ``since=`` read path
    # (R5-D-1). SQLite (community) ignores ``postgresql_using`` → plain index.
    Index("idx_store_audit_created", "created_at", postgresql_using="brin"),
    # BTREE on ``persona_id`` for the ``.read(persona_id, …)`` equality path.
    Index("idx_store_audit_persona", "persona_id"),
)

# Spec R5 (R5-D-1) — multi-worker-safe TOOL audit. Mirrors
# ``persona.tools.audit.ToolAuditEvent`` (file_write / MCP connect-disconnect /
# code-exec ``execute``). Same single-writer JSONL flaw, same Postgres fix.
# ``persona_id`` is nullable (CLI dev sessions carry no persona). NON-RLS
# platform table, plain engine. Distinct from ``store_audit_events`` (store
# mutations) and ``audit_log`` (API actions).
tool_audit_events = Table(
    "tool_audit_events",
    metadata,
    Column("id", Text, primary_key=True, server_default=_uuid_pk),
    Column("timestamp", DateTime(timezone=True), nullable=False),
    Column("persona_id", Text),
    Column("tool_name", Text, nullable=False),
    Column("action", Text, nullable=False),
    Column("resource", Text, nullable=False),
    Column("is_error", Boolean, nullable=False, server_default=text("FALSE")),
    Column("metadata", _json(), nullable=False, server_default=text("'{}'")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Index("idx_tool_audit_created", "created_at", postgresql_using="brin"),
    Index("idx_tool_audit_persona", "persona_id"),
)

# Spec R5 (R5-D-3) — per-request telemetry for the §6.3 "system health" dashboard
# (the D-11-5 gap: ``turn_logs`` has no per-endpoint / HTTP-status / error
# dimension, and ``latency_ms`` is per-TURN model latency, not per-ENDPOINT p99).
# One row per HTTP request, written by ``RequestTelemetryMiddleware`` via a
# BUFFERED background flush — never synchronously on the request path (R5-D-3).
# NON-RLS platform table (operator forensics; read by the ``grafana_ro BYPASSRLS``
# role), plain engine, like ``turn_logs`` / ``audit_log``. ``timestamp`` is the
# request-completion moment captured in-band (NOT the flush time — the buffer
# flushes seconds later); ``route_template`` is the matched route pattern
# (e.g. ``/v1/personas/{persona_id}``) NOT the raw path, to bound cardinality.
request_telemetry = Table(
    "request_telemetry",
    metadata,
    Column("id", Text, primary_key=True, server_default=_uuid_pk),
    Column("timestamp", DateTime(timezone=True), nullable=False),
    Column("method", Text, nullable=False),
    Column("route_template", Text, nullable=False),
    Column("status_code", Integer, nullable=False),
    Column("duration_ms", Float, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    # BRIN on the append-only, time-ascending ``timestamp`` for the windowed
    # percentile_cont / group-by scans the §6.3 panels run (R5-D-3). SQLite
    # ignores ``postgresql_using`` → plain index.
    Index("idx_request_telemetry_ts", "timestamp", postgresql_using="brin"),
    # BTREE on ``route_template`` for the per-endpoint GROUP BY.
    Index("idx_request_telemetry_route", "route_template"),
)

# Spec 30 (D-30-3) — bring-your-own MCP servers. User-scoped (reusable across the
# user's personas), RLS-keyed to owner_id. The user supplies an outbound URL the
# runtime connects to → SSRF-sensitive (validated at the route + at connect, T08).
# Credentials are encrypted at rest (Fernet, T07) in ``credentials_encrypted`` —
# NEVER stored or logged in plaintext; ``auth_method`` ∈ {none, bearer, header}.
# ``discovered_tools`` caches the eager-on-add discovery (D-30-5) for the UI.
user_mcp_servers = Table(
    "user_mcp_servers",
    metadata,
    Column("id", Text, primary_key=True, server_default=_uuid_pk),
    Column("owner_id", Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("name", Text, nullable=False),
    Column("url", Text, nullable=False),
    Column("auth_method", Text, nullable=False, server_default=text("'none'")),
    # Fernet token (T07). NULL when auth_method = 'none'. Never plaintext, never logged.
    Column("credentials_encrypted", Text),
    Column("enabled", Boolean, nullable=False, server_default=text("true")),
    # Cached tool list from eager discovery on add/test (D-30-5); refreshed lazily.
    Column("discovered_tools", _json()),
    # Provenance (Spec N4, N4-D-9): the catalog entry name a self-extension adoption
    # came from (e.g. ``notion-remote``); NULL = a manually-added BYO server. Additive,
    # nullable — the pre-N4 BYO path is byte-identical with this NULL. Used for the
    # vetted-set check (N4-D-6) + an honest "self-extended" UI marker; carries no secret.
    Column("catalog_source", Text),
    # Spec R8 (R8-D-4) — per-user OAuth 2.1 token lifecycle. ALL additive + nullable, so a
    # BYO/PAT row (auth_method ∈ none/bearer/header) is byte-identical with these NULL.
    # ``auth_method = 'oauth'`` selects this path. The access token reuses the existing
    # ``credentials_encrypted`` blob (same Fernet cipher, MCP_CREDENTIAL_KEY — one
    # credential channel, no second key). NEVER plaintext, NEVER logged, NEVER over the API.
    #   oauth_provider          — the provider-registry key (e.g. ``github``); the
    #                             ``mcp-native`` sentinel is reserved for the T7 seam.
    #   refresh_token_encrypted — Fernet token; the rotating refresh token (R8-D-5).
    #   access_token_expires_at — when the access token lapses; drives refresh-before-inject
    #                             (R8-D-5) + reconnect-on-401 (T8).
    #   oauth_scopes            — the granted scopes (display + re-auth), space-delimited.
    Column("oauth_provider", Text),
    Column("refresh_token_encrypted", Text),
    Column("access_token_expires_at", DateTime(timezone=True)),
    Column("oauth_scopes", Text),
    # Spec R8 (T7 generic seam): the RFC 7591 DCR-issued client_id for an
    # ``mcp-native`` discovered server. It CANNOT be re-derived (re-registering yields a
    # new one), so it is persisted; the authorize/token endpoints ARE re-discovered from
    # ``url`` each time. NULL for pre-registered providers (GitHub's client_id is env,
    # not per-row) and non-oauth rows. Additive-nullable (folded into 026 per R8-D-4).
    Column("oauth_client_id", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    # No duplicate server names per user (the name keys the mcp:<name>: prefix).
    UniqueConstraint("owner_id", "name", name="uq_user_mcp_servers_owner_name"),
    Index("idx_user_mcp_servers_owner", "owner_id"),
)

# Spec R8 (R8-D-6) — in-flight OAuth authorization state (the CSRF/PKCE binding).
# One short-lived row per started flow, minted at /authorize and consumed once at
# /callback. RLS-keyed to owner_id (its RLS lives ENTIRELY in migration
# ``027_mcp_oauth`` — the 009/011 pattern — so ``001``'s downgrade never ALTERs a
# later table; deliberately NOT in ``db.rls._POLICIES``). The opaque ``state`` is the
# callback lookup key; the PKCE ``code_verifier`` is Fernet-encrypted at rest (same
# key as the token blobs — a leaked in-flight verifier + code = a token). The redirect
# target is stored server-side here, NEVER encoded in ``state`` (R8-D-6). ``expires_at``
# gives the short TTL; the row is deleted on consume (one-time use) → fail-closed.
mcp_oauth_states = Table(
    "mcp_oauth_states",
    metadata,
    Column("id", Text, primary_key=True, server_default=_uuid_pk),
    Column("owner_id", Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column(
        "server_id",
        Text,
        ForeignKey("user_mcp_servers.id", ondelete="CASCADE"),
        nullable=False,
    ),
    # Opaque high-entropy CSRF token (secrets.token_urlsafe). The callback lookup key.
    Column("state", Text, nullable=False),
    # PKCE code_verifier, Fernet-encrypted at rest (never plaintext). Sent on the
    # back-channel token exchange only; the AS recomputes SHA256 and rejects a mismatch.
    Column("code_verifier_encrypted", Text, nullable=False),
    # The provider-registry key (or ``mcp-native`` sentinel, T7) this flow targets.
    Column("provider", Text, nullable=False),
    # Where to send the user's browser after a successful callback — mapped SERVER-SIDE
    # (never encoded in ``state``). NULL → the default post-connect landing.
    Column("redirect_after", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    # Short TTL — a callback past this is rejected + the row swept (fail-closed).
    Column("expires_at", DateTime(timezone=True), nullable=False),
    # ``state`` is the global callback key → must be unique.
    UniqueConstraint("state", name="uq_mcp_oauth_states_state"),
    Index("idx_mcp_oauth_states_owner", "owner_id"),
)

# Spec 30 (D-30-6) — persona ↔ BYO-server assignment. Many-personas-to-one-server
# so the YAML stays credential-free (no raw URLs in the versioned persona). The
# toolbox resolves a persona's assignments to ``mcp:<server>:<tool>`` at load (T10).
persona_mcp_assignments = Table(
    "persona_mcp_assignments",
    metadata,
    Column("persona_id", Text, ForeignKey("personas.id", ondelete="CASCADE"), nullable=False),
    Column(
        "server_id",
        Text,
        ForeignKey("user_mcp_servers.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    PrimaryKeyConstraint("persona_id", "server_id", name="pk_persona_mcp_assignments"),
    Index("idx_persona_mcp_assignments_server", "server_id"),
)

# ---------------------------------------------------------------------------
# Spec S3 — per-persona speciality (skill) consent (S1-D-6 / S3-D-1).
#
# Append-only consent EVENTS: one row per grant/revoke, NEVER updated — the
# consent history survives (granted at hash H, revoked, re-granted at H' after a
# body change). Current consent = the latest row for
# (persona_id, skill_name, content_hash) with ``granted = true``; **absence →
# denied** (default-deny, S1-D-4). Consent binds to the SKILL.md body hash
# (``content_hash``, S1-D-5), so a synced body change → new hash → no matching
# row → the runtime re-gates. Owner-scoped via the persona FK-chain.
#
# Its RLS lives ENTIRELY in migration ``026_skill_consents`` (mirroring 009/011 —
# so ``001``'s downgrade never ALTERs a later table) and is therefore NOT in
# ``db.rls._POLICIES``. It is a plain relational table (no vectors/FTS), so it is
# available in BOTH editions — consent gating matters single-user too (community
# simply runs it without RLS, like every other table there).
# ---------------------------------------------------------------------------
skill_consents = Table(
    "skill_consents",
    metadata,
    Column("id", Text, primary_key=True, server_default=_uuid_pk),
    Column("persona_id", Text, ForeignKey("personas.id", ondelete="CASCADE"), nullable=False),
    Column("skill_name", Text, nullable=False),
    Column("content_hash", Text, nullable=False),
    Column("granted", Boolean, nullable=False),
    Column("granted_by", Text, nullable=False, server_default=text("'user'")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    # The hot query: the latest event for (persona, skill, current hash).
    Index("idx_skill_consents_lookup", "persona_id", "skill_name", "content_hash", "created_at"),
)

# ---------------------------------------------------------------------------
# Spec K0 — the user-scoped knowledge graph (direction 3).
#
# Three Postgres-only tables (pgvector ``Vector`` + ``tsvector``); user-scoped via
# direct ``owner_id`` (NOT the persona FK-chain — the graph is per *user*). Their
# RLS lives ENTIRELY in migration ``011_knowledge_graph`` (mirroring migration
# 009 — so ``001``'s downgrade never ALTERs a later table) and is therefore NOT
# in ``db.rls._POLICIES``. They are excluded from the community SQLite build
# (``db.community._CLOUD_ONLY_TABLES``) — vectors/FTS are cloud-only, like
# ``memory_chunks``. The persona-core transport (``persona.graph._schema``)
# defines its own view of these; a contract test asserts the two agree (D-K0-3).
# ---------------------------------------------------------------------------

graph_nodes = Table(
    "graph_nodes",
    metadata,
    Column("id", Text, primary_key=True),
    # The turbovec uint64 index key (D-K0-3): collision-free + monotonic.
    Column("surrogate", BigInteger, Identity(always=True), nullable=False, unique=True),
    Column("owner_id", Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("node_kind", Text, nullable=False),
    Column("concept_name", Text, nullable=False),
    Column("content", Text, nullable=False),
    Column("metadata", JSONB, nullable=False),
    Column("wellbeing_category", Text),
    Column("embedding", Vector(EMBEDDING_DIM), nullable=False),
    Column("embedding_model", Text, nullable=False),
    Column("content_hash", Text, nullable=False),
    # The accumulation trail (D-K0-4): a JSONB array of NodeProvenance dumps.
    Column("provenance", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column(
        "fts",
        TSVECTOR,
        Computed("to_tsvector('english', concept_name || ' ' || content)", persisted=True),
    ),
    # Lets graph_edges reference (id, owner_id) so an edge can never cross tenants.
    UniqueConstraint("id", "owner_id", name="uq_graph_nodes_id_owner"),
    Index("ix_graph_nodes_owner", "owner_id"),
    Index(
        "ix_graph_nodes_embedding_hnsw",
        "embedding",
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    ),
    Index("ix_graph_nodes_fts", "fts", postgresql_using="gin"),
)

graph_edges = Table(
    "graph_edges",
    metadata,
    Column("id", Text, primary_key=True),
    Column("owner_id", Text, nullable=False),
    Column("src_node_id", Text, nullable=False),
    Column("dst_node_id", Text, nullable=False),
    Column("link_type", Text, nullable=False),
    Column("weight", REAL),
    Column("provenance", JSONB),
    Column("created_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "link_type IN ('semantic', 'entity', 'temporal', 'causal')",
        name="graph_edges_link_type_check",
    ),
    # Composite FKs: both endpoints belong to the SAME owner (finding-1
    # defense-in-depth); ON DELETE CASCADE removes a node's edges with it.
    ForeignKeyConstraint(
        ["src_node_id", "owner_id"],
        ["graph_nodes.id", "graph_nodes.owner_id"],
        ondelete="CASCADE",
        name="fk_graph_edges_src_owner",
    ),
    ForeignKeyConstraint(
        ["dst_node_id", "owner_id"],
        ["graph_nodes.id", "graph_nodes.owner_id"],
        ondelete="CASCADE",
        name="fk_graph_edges_dst_owner",
    ),
    Index("ix_graph_edges_src", "owner_id", "src_node_id", "link_type"),
    Index("ix_graph_edges_dst", "owner_id", "dst_node_id", "link_type"),
)

graph_entities = Table(
    "graph_entities",
    metadata,
    Column("id", Text, primary_key=True),
    Column("owner_id", Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("canonical_name", Text, nullable=False),
    Column("aliases", JSONB, nullable=False),
    Column("name_embedding", Vector(EMBEDDING_DIM), nullable=False),
    Column("provenance", JSONB),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("id", "owner_id", name="uq_graph_entities_id_owner"),
    Index("ix_graph_entities_owner", "owner_id"),
    Index(
        "ix_graph_entities_name_hnsw",
        "name_embedding",
        postgresql_using="hnsw",
        postgresql_ops={"name_embedding": "vector_cosine_ops"},
    ),
)

# node ↔ canonical-entity associations (Spec K0, T6b). Substrate for entity links
# (criterion 2); ENTITY traversal resolves through this table on-the-fly (no
# materialized node↔node entity edges). RLS via direct owner_id (migration 011).
graph_node_entities = Table(
    "graph_node_entities",
    metadata,
    Column("owner_id", Text, nullable=False),
    Column("node_id", Text, nullable=False),
    Column("entity_id", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    PrimaryKeyConstraint("node_id", "entity_id", name="pk_graph_node_entities"),
    ForeignKeyConstraint(
        ["node_id", "owner_id"],
        ["graph_nodes.id", "graph_nodes.owner_id"],
        ondelete="CASCADE",
        name="fk_gne_node_owner",
    ),
    ForeignKeyConstraint(
        ["entity_id", "owner_id"],
        ["graph_entities.id", "graph_entities.owner_id"],
        ondelete="CASCADE",
        name="fk_gne_entity_owner",
    ),
    Index("ix_gne_entity", "owner_id", "entity_id"),
    Index("ix_gne_node", "owner_id", "node_id"),
)

# --- Connector framework (Spec C1, the connector_identity_linking migration) ---
# The identity-mapping security spine: a one-time link token binds a platform
# identity to a Persona user; thereafter every inbound resolves through a live
# active binding (D-C1-5). Both tables are owner-scoped + RLS like every tenant
# table (the connector process connects as the persona_app non-superuser role, so
# a missed scope fails CLOSED). Declared here (split-home) so a fresh-install 001
# create_all makes them; the migration adds them to a deployed DB. RLS-policy
# lifecycle is owned by the migration, NOT persona_api.db.rls._POLICIES (the
# 009/011/012 self-contained discipline).

# The one-time link handshake token. The PLAINTEXT token is the bearer capability
# handed to the user (and presented on the platform); only its sha256 hex is
# stored (``token_hash``) — a DB leak must not yield usable tokens (the BYO-Fernet
# at-rest posture). Redemption hashes the presented token and looks up by hash.
# Issued by the authenticated owner (RLS-scoped write); redeemed by an
# unauthenticated platform event (cross-tenant read by the unguessable hash via
# the dispatch engine, then owner-scoped — the A0-worker pre-auth pattern).
connector_link_tokens = Table(
    "connector_link_tokens",
    metadata,
    Column("id", Text, primary_key=True, server_default=_uuid_pk),
    Column("token_hash", Text, nullable=False),
    Column("owner_id", Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    # Opaque platform key (D-08-3) — never branched on.
    Column("platform", Text, nullable=False),
    Column("status", Text, nullable=False, server_default=text("'pending'")),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("consumed_at", DateTime(timezone=True)),
    CheckConstraint(
        "status IN ('pending', 'consumed', 'expired')",
        name="connector_link_tokens_status_check",
    ),
    # The capability lookup key — the unguessable hash is unique (single-use is
    # enforced by the status transition pending→consumed, not by the constraint).
    UniqueConstraint("token_hash", name="uq_connector_link_tokens_token_hash"),
    Index("idx_connector_link_tokens_owner", "owner_id"),
)

# The platform-identity ↔ Persona-user binding (the security spine). Resolution
# reads this cross-tenant by (platform, platform_identity) via the dispatch engine
# (pre-auth — the inbound sender is not yet an authenticated owner), then scopes
# everything downstream to the resolved owner. The partial-active unique index
# enforces "one ACTIVE owner per platform identity" while keeping revoked rows for
# audit and allowing re-link after unlink (D-C1-5).
connector_identities = Table(
    "connector_identities",
    metadata,
    Column("id", Text, primary_key=True, server_default=_uuid_pk),
    Column("platform", Text, nullable=False),
    # Opaque string — allows a composite identity (e.g. Slack's (team_id, user_id)).
    Column("platform_identity", Text, nullable=False),
    Column("owner_id", Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("status", Text, nullable=False, server_default=text("'active'")),
    Column("linked_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("revoked_at", DateTime(timezone=True)),
    CheckConstraint(
        "status IN ('active', 'revoked')",
        name="connector_identities_status_check",
    ),
    # PARTIAL unique: one ACTIVE binding per platform identity. A full unique would
    # block re-link-after-unlink forever; this allows revoked rows to remain (audit)
    # while guaranteeing at most one active owner — the cross-user-breach guard.
    Index(
        "uq_connector_identities_active",
        "platform",
        "platform_identity",
        unique=True,
        postgresql_where=text("status = 'active'"),
        sqlite_where=text("status = 'active'"),
    ),
    Index("idx_connector_identities_owner", "owner_id"),
)

# --- Connector conversation state (Spec C1, the connector_conversation_state migration) ---
# The per-persona parallel-conversation model (§3, the agentic-future linchpin):
# each persona has at most one active conversation per user per channel; naming a
# persona foregrounds it and SUSPENDS (never ends) the previously-active one. Two
# additive owner-scoped tables; both under RLS. The state-machine LOGIC
# (foreground/suspend/resume/never-reset) is a later task — these are the records.

# The active-persona pointer: which persona is foregrounded for this (owner,
# platform, channel). One row per channel — the row a switch locks with
# ``SELECT … FOR UPDATE`` to serialise the flip (D-C1-2; the advisory lock is
# txn-scoped and cannot hold state, so the pointer is a durable row).
connector_channels = Table(
    "connector_channels",
    metadata,
    Column("id", Text, primary_key=True, server_default=_uuid_pk),
    Column("owner_id", Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    # Opaque platform key (D-08-3) + the platform conversation key.
    Column("platform", Text, nullable=False),
    Column("channel_key", Text, nullable=False),
    # The foregrounded persona; NULL before any persona is named on this channel.
    Column("active_persona_id", Text),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    # Tenant-consistent persona reference (matches the conversations table pattern).
    # NULL active_persona_id ⇒ MATCH SIMPLE skips the check (no active persona yet).
    ForeignKeyConstraint(
        ["active_persona_id", "owner_id"],
        ["personas.id", "personas.owner_id"],
        ondelete="SET NULL",
        name="fk_connector_channels_active_persona",
    ),
    # One pointer row per (owner, platform, channel) — the FOR UPDATE flip target.
    UniqueConstraint(
        "owner_id", "platform", "channel_key", name="uq_connector_channels_owner_platform_channel"
    ),
    Index("idx_connector_channels_owner", "owner_id"),
)

# Per-persona parallel conversations: one row per (owner, platform, channel,
# persona), each pointing at a real ``conversations`` row. Multiple rows per
# channel = the parallel model (Astrid's conversation and Kai's both persist).
# ``status`` carries suspend/resume/ended; ``last_activity_at`` is the idle timer.
connector_conversations = Table(
    "connector_conversations",
    metadata,
    Column("id", Text, primary_key=True, server_default=_uuid_pk),
    Column("owner_id", Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("platform", Text, nullable=False),
    Column("channel_key", Text, nullable=False),
    Column("persona_id", Text, nullable=False),
    # Additive FK to the real conversation — NO column added to ``conversations``
    # (K2 reads that table; keep it untouched). Single-column FK (conversations has
    # no (id, owner_id) composite unique); owner scoping is via this table's RLS.
    Column(
        "conversation_id",
        Text,
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("status", Text, nullable=False, server_default=text("'active'")),
    Column("last_activity_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(
        "status IN ('active', 'suspended', 'ended')",
        name="connector_conversations_status_check",
    ),
    ForeignKeyConstraint(
        ["persona_id", "owner_id"],
        ["personas.id", "personas.owner_id"],
        ondelete="CASCADE",
        name="fk_connector_conversations_persona",
    ),
    # The per-persona-parallel invariant: at most one conversation row per persona
    # per (owner, platform, channel).
    UniqueConstraint(
        "owner_id",
        "platform",
        "channel_key",
        "persona_id",
        name="uq_connector_conversations_owner_platform_channel_persona",
    ),
    # 1:1 routing-slot guard: a link row is the current slot for exactly one
    # conversation (resume reuses the same row; /new reassigns conversation_id on
    # the one slot — history stays in conversations/messages). UNIQUE so a T6
    # state-machine bug can never double-link two slots to one conversation.
    UniqueConstraint("conversation_id", name="uq_connector_conversations_conversation"),
    Index("idx_connector_conversations_owner", "owner_id"),
    # The idle-timeout sweep (Spec C1 T8): end live slots idle past the cutoff —
    # ``WHERE status IN ('active','suspended') AND last_activity_at < cutoff``.
    Index("idx_connector_conversations_idle", "status", "last_activity_at"),
)


# Spec K2 (T8) — the per-interaction synthesis idempotency marker (D-K2-X-migration-
# placeholder). Channel-agnostic (web chat / agentic run / voice) so all three
# synthesis feeders share one high-water-mark surface. ``synthesised_up_to`` mirrors
# ``conversations.compacted_up_to``: synthesis processes only content past it, then
# advances it in the same owner-scoped txn — the second idempotency line behind A0's
# idempotency key (criterion 8). Created with its RLS ENTIRELY in its own migration
# (the 009/011/012 template); deliberately NOT in ``db/rls._POLICIES``.
synthesis_markers = Table(
    "synthesis_markers",
    metadata,
    Column("id", Text, primary_key=True, server_default=_uuid_pk),
    Column("owner_id", Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    # 'conversation' | 'agentic_run' | 'voice' — the channel-agnostic discriminator.
    Column("interaction_kind", Text, nullable=False),
    # conversation_id | run_id | voice-session-id — the source interaction's id.
    Column("interaction_id", Text, nullable=False),
    # High-water-mark of synthesised progress (mirrors conversations.compacted_up_to).
    Column("synthesised_up_to", Integer, nullable=False, server_default=text("0")),
    Column("synthesised_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(
        "interaction_kind IN ('conversation', 'agentic_run', 'voice')",
        name="synthesis_markers_kind_check",
    ),
    # One marker per (owner, kind, interaction) — the compare-and-set anchor.
    UniqueConstraint(
        "owner_id",
        "interaction_kind",
        "interaction_id",
        name="uq_synthesis_markers_owner_kind_interaction",
    ),
    Index("idx_synthesis_markers_owner", "owner_id"),
)


# Spec V9 (V9-D-5) — the durable call-record envelope (migration 021). A voice
# call's lifecycle metadata — persisted NOWHERE server-side before V9 (it lived
# only in the in-memory ``SessionStateMachine`` + client storage). The voice
# runtime is API-free, so it WRITES this via a core-owned ``_calls`` Table view
# (``persona.calls``) on its session RLS engine (the ``memory_chunks`` P2
# precedent); a contract test guards the view↔DDL drift. The Calls surface READS
# it (the call-record is the Calls-membership key — V9-D-3 — NOT ``origin``).
# RLS lives ENTIRELY in this table's own migration (the 009/011/012/015
# template); deliberately NOT in ``db/rls._POLICIES``.
calls = Table(
    "calls",
    metadata,
    Column("call_id", Text, primary_key=True),
    Column(
        "conversation_id",
        Text,
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("persona_id", Text, nullable=False),
    # RLS anchor: every policy below scopes on owner_id (mirrors conversations).
    Column("owner_id", Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    # NULL while the call is live / on a crash with no clean end.
    Column("ended_at", DateTime(timezone=True)),
    # STORED, not derived (V9-D-5): a live/crashed call stays queryable and the
    # Calls list avoids a per-row ``ended_at - started_at`` compute. Set at close.
    Column("duration_s", Integer),
    # NULL while live; set at close. v1 writes 'disconnect' (clean room end) or
    # 'error' (crash); 'user_hangup'/'switched' are reserved for a later web-side
    # refinement (the server only sees a room disconnect today).
    Column("end_reason", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(
        "end_reason IS NULL OR end_reason IN ('user_hangup', 'switched', 'error', 'disconnect')",
        name="calls_end_reason_check",
    ),
    Index("idx_calls_owner", "owner_id"),
    Index("idx_calls_conversation", "conversation_id"),
)


# The autonomous task model (Spec A2). A ``task`` is the durable entity above runs;
# it spans days through many bounded agentic legs, each executed as an A0 job. Both
# tables are owner-scoped + RLS like every tenant table (the leg handler sets the
# owner GUC at job-execution top — D-A0-X-rls-chokepoint — and operates RLS-as-owner;
# no ``job_dispatcher`` grant, matching synthesis_markers). Low-volume entity tables
# (one row per task; one append per leg) → default autovacuum, no queue reloptions.
tasks = Table(
    "tasks",
    metadata,
    Column("id", Text, primary_key=True, server_default=_uuid_pk),
    Column("owner_id", Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("persona_id", Text, ForeignKey("personas.id", ondelete="CASCADE"), nullable=False),
    # The frozen, A4-authored Contract (goal/scope/criteria/bounds). Written once at
    # create, never updated by any leg path — immutability is app-enforced (only A4's
    # amendment flow writes it; out of scope here). Stored whole as JSONB.
    Column("contract_json", _json(), nullable=False),
    Column("state", Text, nullable=False, server_default=text("'defined'")),
    Column("paused", Boolean, nullable=False, server_default=text("false")),
    # Set iff state='waiting' (the wait kind); enforced by tasks_wait_kind_iff_waiting.
    Column("wait_kind", Text),
    # The cost ledger (A2 accounts what A0 meters; A3 enforces against the SUM, A6 shows
    # per-kind). BigInteger: a multi-day task can accumulate past Integer's ceiling.
    Column("ledger_model_micros", BigInteger, nullable=False, server_default=text("0")),
    Column("ledger_sandbox_micros", BigInteger, nullable=False, server_default=text("0")),
    Column("ledger_external_micros", BigInteger, nullable=False, server_default=text("0")),
    # The latest committed checkpoint sequence — the CAS target (NULL before leg one).
    Column("head_checkpoint_seq", Integer),
    # Linkage points A4/A6 consume.
    Column("conversation_id", Text, ForeignKey("conversations.id", ondelete="SET NULL")),
    Column("run_ids", _json(), nullable=False, server_default=text("'[]'")),
    Column("workspace_id", Text),
    Column("schedule_id", Text, ForeignKey("schedules.id", ondelete="SET NULL")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("schema_version", Text, nullable=False, server_default=text("'1.0'")),
    CheckConstraint(
        "state IN ('defined','active','waiting','completed','failed','cancelled')",
        name="tasks_state_check",
    ),
    CheckConstraint(
        "wait_kind IS NULL OR wait_kind IN ('until_time','on_user','on_event')",
        name="tasks_wait_kind_check",
    ),
    # Defence-in-depth: the T2 model_validator invariant (wait_kind set iff WAITING)
    # holds even for a direct SQL write (mirrors schedules' XOR check).
    CheckConstraint(
        "(wait_kind IS NOT NULL) = (state = 'waiting')",
        name="tasks_wait_kind_iff_waiting",
    ),
    CheckConstraint(
        "ledger_model_micros >= 0 AND ledger_sandbox_micros >= 0 AND ledger_external_micros >= 0",
        name="tasks_ledger_nonneg",
    ),
    CheckConstraint(
        "head_checkpoint_seq IS NULL OR head_checkpoint_seq >= 0",
        name="tasks_head_seq_nonneg",
    ),
    Index("idx_tasks_owner", "owner_id"),
    Index("idx_tasks_persona", "persona_id"),
    # Find runnable/resumable tasks (the worker + A6).
    Index("idx_tasks_active", "state", postgresql_where=text("state IN ('active','waiting')")),
    # Resolve a schedule fire → its task.
    Index("idx_tasks_schedule", "schedule_id", postgresql_where=text("schedule_id IS NOT NULL")),
)

# Append-only checkpoint sequence per task — the durable half of A2-R-4. The
# UNIQUE(task_id, checkpoint_seq) is the compare-and-set anchor: a re-delivered leg's
# duplicate (task_id, seq) INSERT no-ops via ON CONFLICT, while the tasks.head CAS
# (UPDATE ... WHERE head_checkpoint_seq IS NOT DISTINCT FROM :predecessor) advances
# exactly once. The composite UNIQUE (task_id leading) also serves the latest-checkpoint
# read (WHERE task_id=? ORDER BY checkpoint_seq DESC LIMIT 1) — no extra index.
task_checkpoints = Table(
    "task_checkpoints",
    metadata,
    Column("id", Text, primary_key=True, server_default=_uuid_pk),
    Column("task_id", Text, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
    Column("owner_id", Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("checkpoint_seq", Integer, nullable=False),
    # The frozen TaskCheckpoint (conclusions/decisions/lessons/plan/next_step/open
    # questions/pointers/cursor) — read whole at reconstruction, never sub-field-queried.
    Column("checkpoint_json", _json(), nullable=False),
    Column("content_hash", Text, nullable=False),
    Column("schema_version", Text, nullable=False, server_default=text("'1.0'")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("checkpoint_seq >= 0", name="task_checkpoints_seq_nonneg"),
    UniqueConstraint("task_id", "checkpoint_seq", name="uq_task_checkpoints_task_seq"),
    Index("idx_task_checkpoints_owner", "owner_id"),
)

# Spec A3 — the durable approval artifacts (T5). Owner-scoped + RLS like every tenant table:
# the gated leg records an exact proposal, the task parks waiting(on_user), the user's reply
# becomes a decision, and an approved proposal is replayed verbatim. Budget lifecycle is NOT a
# table here — it rides audit_log rows (action='budget.*'), and the cap lives on the contract
# (ContractBounds.total_budget_micros) + SUM of budget.extended rows (A3-D-X-migration).
approval_proposals = Table(
    "approval_proposals",
    metadata,
    Column("id", Text, primary_key=True, server_default=_uuid_pk),
    Column("owner_id", Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("task_id", Text, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
    Column("persona_id", Text, ForeignKey("personas.id", ondelete="CASCADE"), nullable=False),
    Column("tool_name", Text, nullable=False),
    # The EXACT recorded payload — replayed verbatim on approval, never re-derived by the model
    # (A3-D-X-approved-execution). The human-readable rendering is `description`.
    Column("arguments_json", _json(), nullable=False, server_default=text("'{}'")),
    # The tool's resolved ActionCategory set that triggered the gate (a JSON array of strings).
    Column("categories_json", _json(), nullable=False, server_default=text("'[]'")),
    Column("description", Text, nullable=False),
    Column("status", Text, nullable=False, server_default=text("'pending'")),
    # created_at drives the expiry sweep (A3-D-2); updated_at is the status-change time.
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    # The remind-once marker (A3-D-2, T9): the sweep CASes it NULL→now so a 24h reminder fires
    # exactly once; status stays 'pending' (the one-pending index is undisturbed).
    Column("reminded_at", DateTime(timezone=True), nullable=True),
    CheckConstraint(
        "status IN ('pending','approved','denied','modified','expired','consumed')",
        name="approval_proposals_status_valid",
    ),
    # The STRUCTURAL one-pending-per-task guarantee (criterion 4): the DB makes "two open
    # proposals on one task" impossible. 'pending' is the sole open state — a material
    # re-confirm updates the row in place rather than opening a second proposal.
    Index(
        "uq_one_pending_proposal_per_task",
        "task_id",
        unique=True,
        postgresql_where=text("status = 'pending'"),
    ),
    Index("idx_approval_proposals_owner", "owner_id"),
    Index("idx_approval_proposals_created", "created_at"),
    # The composite-FK target for approval_decisions — pins a decision's denormalized owner_id
    # to its proposal's owner (a decision can never reference a proposal owned by someone else).
    UniqueConstraint("owner_id", "id", name="uq_approval_proposals_owner_id"),
)

# Append-only: a clarify-then-approve sequence keeps BOTH rows (the full audit trail,
# criterion 9). owner_id is denormalized from the proposal for the RLS predicate (it must
# always equal the proposal's owner — the WITH CHECK + the proposal's own scope keep them
# aligned). edited_arguments_json is set iff type='modify' (the T3 pydantic model is the gate;
# the column stays nullable, no DB CHECK).
approval_decisions = Table(
    "approval_decisions",
    metadata,
    Column("id", Text, primary_key=True, server_default=_uuid_pk),
    Column("owner_id", Text, nullable=False),
    Column("proposal_id", Text, nullable=False),
    Column("type", Text, nullable=False),
    Column("verbatim_reply", Text, nullable=False),
    Column("channel", Text, nullable=False),
    Column("edited_arguments_json", _json(), nullable=True),
    Column("decided_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(
        "type IN ('approve','deny','modify','clarify')", name="approval_decisions_type_valid"
    ),
    # The composite FK structurally pins owner_id to the proposal's owner (a cross-owner
    # decision is impossible, not merely RLS-checked) and CASCADE-deletes with the proposal.
    # owner_id's referential integrity to users is transitive via the proposal's own FK.
    ForeignKeyConstraint(
        ["owner_id", "proposal_id"],
        ["approval_proposals.owner_id", "approval_proposals.id"],
        ondelete="CASCADE",
        name="fk_approval_decisions_proposal_owner",
    ),
    Index("idx_approval_decisions_owner", "owner_id"),
    Index("idx_approval_decisions_proposal", "proposal_id"),
)

# Spec A3 — the kill switches (T11). Two independent pause sources at their natural levels, so
# clearing one (budget's task `paused` overlay, T10) can never resume a task another source
# holds (the reason-scoped runnable invariant): persona-suspend is owner-scoped + RLS;
# global-pause is a single operational (ownerless) flag the worker consults before claiming.
suspended_personas = Table(
    "suspended_personas",
    metadata,
    Column("owner_id", Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("persona_id", Text, ForeignKey("personas.id", ondelete="CASCADE"), nullable=False),
    Column("suspended_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    # Presence = suspended; the row is deleted on resume.
    PrimaryKeyConstraint("owner_id", "persona_id", name="pk_suspended_personas"),
)

# Spec A3 — the cadence cap counter (T12). Per-persona/per-day count of *chatter* C0 messages;
# the atomic conditional upsert never exceeds the cap. approval/failure/safety messages bypass
# the cap entirely (never counted here). Owner-scoped + RLS.
cadence_counters = Table(
    "cadence_counters",
    metadata,
    Column("owner_id", Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("persona_id", Text, ForeignKey("personas.id", ondelete="CASCADE"), nullable=False),
    Column("day", Date, nullable=False),
    Column("count", Integer, nullable=False, server_default=text("0")),
    PrimaryKeyConstraint("owner_id", "persona_id", "day", name="pk_cadence_counters"),
)

# The platform-wide autonomy controls — operational, ownerless (the global "big red button":
# stop claiming autonomy jobs platform-wide). NOT RLS-scoped (it is an operator control, not
# tenant data); a single row keyed ``global_autonomy_paused`` the worker reads before claiming.
platform_controls = Table(
    "platform_controls",
    metadata,
    Column("key", Text, primary_key=True),
    Column("enabled", Boolean, nullable=False, server_default=text("false")),
    Column("actor", Text, nullable=True),  # who last toggled it (audit-on-the-row + the audit_log)
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

# Spec P6 (P6-D-11) — the durable, cross-device notification feed.
#
# Backs the bell's server-authored entries (run-terminal, persona-ready) so a
# notification survives reload + syncs across devices — the promotion of Spec
# 35's ``localStorage``-capped-30 client feed. Owner-scoped + RLS like every
# tenant table (the ``persona_app`` non-superuser role is subject to it).
#
# Copy is stored locale-neutral (P6-D-5): ``message_key`` + ``params`` JSONB, the
# web localises at render via next-intl — no server-side i18n. ``kind`` + ``ref_id``
# drive the deep-link (``run_terminal`` → ``/runs/{ref_id}``; ``persona_ready`` →
# ``/personas/{ref_id}``) and, with ``owner_id``, form the idempotency key that
# makes a server-authored write once-only across the run's persist-final /
# persist-error / restart-sweep paths (P6-D-11/D-P6-12).
notifications = Table(
    "notifications",
    metadata,
    Column("id", Text, primary_key=True, server_default=_uuid_pk),
    Column("owner_id", Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("kind", Text, nullable=False),
    # The deep-link target's id (a run_id or persona_id). Also the idempotency
    # discriminator with (owner_id, kind); nullable for future ref-less kinds.
    Column("ref_id", Text, nullable=True),
    Column("level", Text, nullable=False),
    Column("message_key", Text, nullable=False),
    Column("params", _json(), nullable=False, server_default=text("'{}'")),
    Column("read", Boolean, nullable=False, server_default=text("false")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(
        "kind IN ('run_terminal', 'persona_ready')",
        name="notifications_kind_check",
    ),
    CheckConstraint(
        "level IN ('success', 'error', 'info', 'warning')",
        name="notifications_level_check",
    ),
    # Idempotency (P6-D-11): one row per (owner, kind, ref) — a retry / restart-
    # sweep re-run of a server-authored write is a no-op via ON CONFLICT.
    UniqueConstraint("owner_id", "kind", "ref_id", name="uq_notifications_owner_kind_ref"),
    # The feed query: newest-first for the current owner (RLS scopes the owner).
    Index("idx_notifications_owner_created", "owner_id", text("created_at DESC")),
)
