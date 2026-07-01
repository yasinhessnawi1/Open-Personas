"""Configuration for persona-api, loaded from environment variables (spec 08, T01).

Twelve-Factor (ENGINEERING_STANDARDS.md §4): every runtime knob is an env var; no
YAML config files, no Hydra. Values are read once at process start via Pydantic
Settings and injected downstream — code accepts an :class:`APIConfig` instance
rather than reading ``os.environ`` directly.

The ``PERSONA_API_`` prefix keeps the API's own knobs distinct from
``persona-core``'s ``PERSONA_`` config (the API constructs a ``PersonaCoreConfig``
separately for the toolbox). ``DATABASE_URL`` / ``APP_DATABASE_URL`` are read
without the prefix to match the spec-07 conventions the migration + Docker harness
already use.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["APIConfig", "Edition"]


class Edition(StrEnum):
    """The open-core edition this process runs as (Spec 33, D-33-1).

    A single ``PERSONA_EDITION`` switch drives every commercial seam
    (``OwnerResolver``, ``CreditsPolicy``, the persistence backend). ``community``
    (the default) is the zero-infra, single-local-owner, no-auth/no-credits
    self-host; ``cloud`` is the owner's commercial hosting — Clerk auth,
    multi-tenant RLS over Postgres, metered credits — reproducing today's
    behavior exactly.
    """

    community = "community"
    cloud = "cloud"


class APIConfig(BaseSettings):
    """Environment-driven configuration for the hosted API.

    Attributes:
        database_url: The superuser/owner DSN used to run migrations and
            (in dev/tests) the store engine. Sync psycopg3 dialect. Read from
            ``DATABASE_URL`` (no prefix — matches spec 07).
        app_database_url: The non-superuser ``persona_app`` DSN the request path
            connects with so RLS is enforced (superusers bypass it). Falls back
            to ``database_url`` when unset (single-role dev). Read from
            ``APP_DATABASE_URL``.
        jwt_secret: Symmetric signing key for HS256 token verification (the
            v0.1/test path; D-08-4). Never logged. Empty disables HS256 verify.
        jwt_public_key: PEM public key for RS256 verification (the real
            Clerk/Supabase JWKS path; D-08-4). Never logged.
        jwt_algorithms: Allowed JWT algorithms (comma-separated).
        jwt_audience: Expected ``aud`` claim; empty skips the audience check.
        embedder_model: Sentence-transformers model for persona memory
            embedding (architecture §9.6; D-08-8). bge-small-en-v1.5 → 384-dim.
        db_pool_size: Connection-pool size for the request engine. >1 so a slow
            sync store call doesn't serialise concurrent CRUD (research §5).
        rate_limit_default: Default per-user-per-endpoint-per-minute limit (§6).
        rate_limit_messages / rate_limit_runs / rate_limit_author: Per-endpoint
            overrides (§6 table).
        authoring_credit_cost: Flat credit deduction per authoring call (D-08-6,
            §11 risk).
    """

    model_config = SettingsConfigDict(
        env_prefix="PERSONA_API_", extra="ignore", populate_by_name=True
    )

    # Open-core edition (Spec 33, D-33-1). Default `community` — the safe,
    # zero-infra self-host. Read from ``PERSONA_EDITION`` (no prefix, so web/api/
    # voice all read the SAME var). `cloud` is the explicit commercial opt-in.
    edition: Edition = Field(default=Edition.community, validation_alias="PERSONA_EDITION")

    # Within-runtime origination (Spec C0, T7, criterion 7). When enabled, a
    # completed agentic run originates its conclusion as a first-class, delivered
    # message (persisted to the conversation + episodic, pushed inline on the run's
    # open stream). Default OFF: the capability is built + proven, but auto-firing
    # on every run is opt-in (it starts a conversation per run — D-C0-3 — a product
    # choice). Read from ``PERSONA_API_WITHIN_RUNTIME_ORIGINATION``.
    within_runtime_origination: bool = Field(default=False)

    # Safety guard (Spec 33, D-33-4 / D-33-X-public-bind-detection): community is
    # no-auth single-user-local by intent. When auth is disabled (community) the
    # API refuses to start on a non-loopback bind unless this is set — a
    # fail-safe against an accidentally-exposed open, unauthenticated instance.
    allow_public_noauth: bool = Field(default=False, validation_alias="PERSONA_ALLOW_PUBLIC_NOAUTH")

    # Spec N1 (D-N1-7): a cloud (multi-tenant) Docker MCP Gateway is connect-only to an
    # operator-run gateway SHARED across tenants, so it must be operator-vetted. Like the
    # public-noauth guard, cloud refuses to start with a gateway URL set unless the
    # operator explicitly acknowledges the vetted-shared posture here. Ignored in
    # community (the user runs their own gateway — the local "setup once" win).
    allow_cloud_gateway: bool = Field(default=False, validation_alias="PERSONA_ALLOW_CLOUD_GATEWAY")

    # The bind host the server listens on. Read by the safety guard (D-33-4) to
    # detect a non-loopback (public) bind under community/no-auth. Loopback by
    # default; a community deploy that sets a public host must also set
    # ``PERSONA_ALLOW_PUBLIC_NOAUTH=1`` or the API refuses to start.
    host: str = "127.0.0.1"

    # The community single-owner identity (D-33-3). All app-table rows belong to
    # this constant owner; seeded as a `users` row at startup (D-33-X-owner-seed).
    community_owner_id: str = "local-owner"
    community_owner_email: str = "local@localhost"

    # Community relational store path (D-33-7): a single SQLite file, zero-setup.
    # Read from ``PERSONA_API_COMMUNITY_DB_PATH``; defaults under the cwd.
    community_db_path: Path = Field(default_factory=lambda: Path.cwd() / ".persona_community.db")

    # Community typed-memory store dir (D-33-X-memory-chroma-community): the
    # file-based Chroma persist path. Read from ``PERSONA_API_COMMUNITY_MEMORY_PATH``.
    community_memory_path: Path = Field(default_factory=lambda: Path.cwd() / ".persona_chroma")

    # DB DSNs — read WITHOUT the prefix (spec-07 convention: DATABASE_URL /
    # APP_DATABASE_URL). validation_alias overrides the env_prefix per field.
    database_url: str = Field(default="", validation_alias="DATABASE_URL", repr=False)
    app_database_url: str = Field(default="", validation_alias="APP_DATABASE_URL", repr=False)
    # Spec A0 (D-A0-X-rls-chokepoint hardening): the worker's CROSS-TENANT
    # dispatch engine DSN — claim/heartbeat/complete on the jobs tables only.
    # Empty → falls back to ``database_url`` (the superuser engine) for v0.1.
    # Point this at a least-privilege ``job_dispatcher`` role (BYPASSRLS, granted
    # on jobs/jobs_archive ONLY, no tenant-table grants) to harden — a pure-config
    # swap, no code change. The role is provisioned out-of-band (D-07-5); migration
    # 011 conditionally grants to it if present.
    worker_dispatch_database_url: str = Field(
        default="", validation_alias="WORKER_DISPATCH_DATABASE_URL", repr=False
    )
    # Spec A0 worker runtime knobs (all config-driven — D-A0-3 concurrency, D-A0-5
    # drain). Poll uses a jittered interval so N workers don't thunder the claim
    # query in lockstep. ``worker_drain_seconds`` must be < Fly's ``kill_timeout``
    # (300s); default 270 leaves a ~30s safety margin (D-A0-5). The claim lease is
    # a bootstrap; per-job-type heartbeat (D-A0-1) extends it during execution.
    worker_concurrency: int = Field(default=4, ge=1, validation_alias="WORKER_CONCURRENCY")
    worker_poll_interval_seconds: float = Field(
        default=1.0, gt=0, validation_alias="WORKER_POLL_INTERVAL_SECONDS"
    )
    worker_poll_jitter_seconds: float = Field(
        default=0.5, ge=0, validation_alias="WORKER_POLL_JITTER_SECONDS"
    )
    worker_claim_lease_seconds: int = Field(
        default=90, gt=0, validation_alias="WORKER_CLAIM_LEASE_SECONDS"
    )
    worker_drain_seconds: float = Field(
        default=270.0, gt=0, validation_alias="WORKER_DRAIN_SECONDS"
    )
    # Claim-time fairness caps (D-A0-6). ``per_user`` (default 3) stops one user's
    # flood from starving others — a user already at the cap has their queued jobs
    # skipped so other users' jobs are claimed. ``global`` (0 = unlimited) caps
    # total in-flight across all users. Anti-starvation gate, not a hard cap.
    worker_max_jobs_per_user: int = Field(
        default=3, ge=1, validation_alias="WORKER_MAX_JOBS_PER_USER"
    )
    worker_max_jobs_global: int = Field(default=0, ge=0, validation_alias="WORKER_MAX_JOBS_GLOBAL")
    # Maintenance sweep cadence (D-A0-4): each worker periodically rescues expired
    # leases (the rescuer), ages terminal jobs older than ``archive_after`` into the
    # cold ``jobs_archive`` (the cleaner — keeps the hot table small), and purges
    # archive rows past ``retention``. All config-driven; defaults: sweep every 30s,
    # archive after 1 day, retain the archive 30 days.
    worker_maintenance_interval_seconds: float = Field(
        default=30.0, gt=0, validation_alias="WORKER_MAINTENANCE_INTERVAL_SECONDS"
    )
    worker_archive_after_seconds: float = Field(
        default=86_400.0, gt=0, validation_alias="WORKER_ARCHIVE_AFTER_SECONDS"
    )
    worker_archive_retention_seconds: float = Field(
        default=2_592_000.0, gt=0, validation_alias="WORKER_ARCHIVE_RETENTION_SECONDS"
    )
    # Spec A1 — the scheduler tick (hosted in the worker; leader-gated). All
    # config-driven (D-A1-2/D-A1-3). The tick runs every ``tick_interval`` (jittered
    # by the worker loop), claims up to ``batch_size`` due schedules, and applies the
    # missed-fire policy: ``on_time_tolerance`` = how late still counts as "caught
    # promptly" (fire regardless of policy); ``default_grace`` / ``one_time_grace`` =
    # the fire-late-once catch-up window (kind-relative — daily ≈ 3h, one-time ≈ 1h),
    # overridable per schedule. Beyond grace (or skip-and-note) → skip + durable note.
    scheduler_tick_interval_seconds: float = Field(
        default=30.0, gt=0, validation_alias="PERSONA_SCHEDULER_TICK_INTERVAL_SECONDS"
    )
    scheduler_batch_size: int = Field(
        default=100, ge=1, validation_alias="PERSONA_SCHEDULER_BATCH_SIZE"
    )
    scheduler_default_grace_seconds: float = Field(
        default=10_800.0, ge=0, validation_alias="PERSONA_SCHEDULER_DEFAULT_GRACE_SECONDS"
    )
    scheduler_one_time_grace_seconds: float = Field(
        default=3_600.0, ge=0, validation_alias="PERSONA_SCHEDULER_ONE_TIME_GRACE_SECONDS"
    )
    scheduler_on_time_tolerance_seconds: float = Field(
        default=120.0, ge=0, validation_alias="PERSONA_SCHEDULER_ON_TIME_TOLERANCE_SECONDS"
    )
    # Spec N2 — the MCP catalog auto-sync (hosted in the worker loop, leader-gated;
    # N2-D-1/2/3). A daily-ish periodic task re-pulls Docker's catalog and reconciles
    # the writable mirror (PERSONA_MCP_MIRROR_PATH). ``enabled`` is the opt-out for
    # local/community deployments that don't want a periodic outbound git clone — OFF →
    # availability stays at the bundled snapshot (fail-soft). The interval defaults to a
    # day (the catalog changes slowly; one shallow clone/day from one process is plenty).
    mcp_catalog_sync_enabled: bool = Field(
        default=True, validation_alias="PERSONA_MCP_SYNC_ENABLED"
    )
    mcp_catalog_sync_interval_seconds: float = Field(
        default=86_400.0, gt=0, validation_alias="PERSONA_MCP_SYNC_INTERVAL_SECONDS"
    )
    # Spec S2 — the external skill-catalog auto-sync (a second mirror on N2's substrate).
    # Disabled by default: availability stays at the last-synced snapshot (fail-soft); enable
    # to keep the external skill mirror fresh. The Anthropic source is the pinned code constant
    # (no config); OpenClaw is opt-in via its curated repo URL (empty ⇒ OpenClaw not synced).
    skill_catalog_sync_enabled: bool = Field(
        default=False, validation_alias="PERSONA_SKILL_SYNC_ENABLED"
    )
    skill_catalog_sync_interval_seconds: float = Field(
        default=86_400.0, gt=0, validation_alias="PERSONA_SKILL_SYNC_INTERVAL_SECONDS"
    )
    skill_openclaw_repo_url: str = Field(
        default="", validation_alias="PERSONA_SKILL_OPENCLAW_REPO_URL"
    )
    skill_openclaw_ref: str = Field(default="", validation_alias="PERSONA_SKILL_OPENCLAW_REF")
    # A0 T9 enqueue→worker cutover flag. OFF (default) → avatar generation runs the
    # legacy in-process BackgroundTasks path (contract unchanged). ON → the create
    # path ENQUEUES a durable avatar job for the worker (survives an api restart).
    # The orchestrator flips this at close-out once the worker is deployed.
    avatar_via_queue: bool = Field(default=False, validation_alias="PERSONA_API_AVATAR_VIA_QUEUE")

    # Spec K2 (T8d) — the synthesis-pipeline activation flags. The deploy is a
    # single uvicorn process (D-08-5), so the durable A0 worker + A1 scheduler tick
    # run as an IN-PROCESS background task started in the lifespan worker root
    # (``background.worker_root``), not a separate machine. OFF (default) keeps the
    # producer enqueues no-op-consumed (the pre-activation state); ON composes the
    # consumer (synthesis + avatar handlers + the scheduler tick) and runs the
    # claim→execute loop alongside the API. The orchestrator flips this on the
    # wired tier once the eval re-run gate is green.
    in_process_worker: bool = Field(default=False, validation_alias="PERSONA_API_IN_PROCESS_WORKER")
    # The tier the synthesis extractor + entity judge run on (D-K2-3). The hard
    # pre-live gate #2 re-runs the extraction corpus eval on THIS tier (NOT the
    # frontier/sonnet tier). ``small`` by default (cheap reflection pass).
    synthesis_tier: str = Field(default="small", validation_alias="PERSONA_API_SYNTHESIS_TIER")
    # On-by-default ``record_user_fact`` direct-write tool (D-K2-1). The persona's
    # per-persona ``tools`` allow-list is still the final gate inside
    # ``build_default_toolbox``; this flag only governs whether the tool is COMPOSED
    # into the toolbox at all. ON by default — the means-redaction backstop is
    # structural (direct_write.py + synthesizer.py both run ``contains_self_harm_means``).
    record_user_fact_enabled: bool = Field(
        default=True, validation_alias="PERSONA_API_RECORD_USER_FACT_ENABLED"
    )

    # Auth (D-08-4). Secrets never logged.
    jwt_secret: SecretStr | None = Field(default=None, repr=False)
    jwt_public_key: SecretStr | None = Field(default=None, repr=False)
    jwt_algorithms: str = "HS256"
    jwt_audience: str = ""

    # Spec 30 T07 (D-30-4) — bring-your-own MCP credential encryption-at-rest.
    # One or more comma-separated url-safe-base64 Fernet keys; the FIRST encrypts,
    # all decrypt (MultiFernet → zero-downtime rotation, documented in
    # MAINTENANCE.md). Unset → BYO-MCP credential storage fails fast at the route
    # (a server with auth cannot be saved without a key). Never logged.
    mcp_credential_key: SecretStr | None = Field(
        default=None, validation_alias="MCP_CREDENTIAL_KEY", repr=False
    )

    # Spec N4 (N4-D-6) — the cloud operator-vetted set a persona may self-adopt from the
    # mirrored catalog. Comma-separated catalog entry names. Community ignores this (any
    # ``type: remote`` entry is adoptable — the user owns the trust choice). Cloud honors
    # it as an allowlist; the **empty default is deny-all (fail-closed)** — nothing is
    # catalog-adoptable in cloud until the operator vets it. Scopes ONLY catalog-discovered
    # adoption (mcp_search → adopt); never the existing built-in/Spec-27/N3 grant path.
    mcp_adopt_vetted: str = Field(default="", validation_alias="PERSONA_MCP_ADOPT_VETTED")

    # Spec R8 — per-user MCP OAuth 2.1 (R8-D-1/6/7). Open Persona is the OAuth *client*.
    #
    # The fixed, pre-registered HTTPS callback base — the OAuth redirect_uri is
    # ``{base}/v1/mcp/oauth/callback`` and is validated EXACTLY (never taken from the
    # form/user). MUST be the https origin registered at each provider (R8-D-6). Empty
    # → OAuth initiate fails closed (no redirect can be built).
    mcp_oauth_redirect_base_url: str = Field(
        default="", validation_alias="PERSONA_MCP_OAUTH_REDIRECT_BASE_URL"
    )
    # GitHub pre-registered client (R8-D-7 / T9.5 operator runbook): the operator
    # one-time registers the Open Persona OAuth app at GitHub (no RFC 7591 DCR there)
    # and sets these. ``client_id`` is public; ``client_secret`` is a secret (repr=False,
    # never logged). Unset ``client_id`` → GitHub is simply not an offered provider.
    mcp_oauth_github_client_id: str = Field(
        default="", validation_alias="PERSONA_MCP_OAUTH_GITHUB_CLIENT_ID"
    )
    mcp_oauth_github_client_secret: SecretStr | None = Field(
        default=None, validation_alias="PERSONA_MCP_OAUTH_GITHUB_CLIENT_SECRET", repr=False
    )
    # Space-delimited OAuth scopes requested for GitHub (least-privilege default: repo).
    mcp_oauth_github_scopes: str = Field(
        default="repo", validation_alias="PERSONA_MCP_OAUTH_GITHUB_SCOPES"
    )
    # State TTL (seconds) — an in-flight OAuth flow must complete the browser
    # round-trip within this window; a callback past it is rejected + swept.
    mcp_oauth_state_ttl_seconds: int = Field(
        default=600, validation_alias="PERSONA_MCP_OAUTH_STATE_TTL_SECONDS"
    )
    # Refresh the access token when it is within this many seconds of expiry at
    # persona-load (refresh-before-inject, R8-D-5) — a safety margin below expiry.
    mcp_oauth_refresh_leeway_seconds: int = Field(
        default=120, validation_alias="PERSONA_MCP_OAUTH_REFRESH_LEEWAY_SECONDS"
    )

    # Memory embedding (D-08-8).
    embedder_model: str = "BAAI/bge-small-en-v1.5"

    # Audit-log root for the store-mutation JSONL audit (spec 01 AuditLogger).
    # Distinct from the api `audit_log` TABLE (T12) — see spec-07 handoff.
    audit_root: str = "./.persona_audit"

    # Connection pool (research §5 — roomy pool removes store/CRUD contention).
    db_pool_size: int = 5

    # Rate limiting (§6). backend: "memory" (dev/tests) or "postgres".
    rate_limit_backend: str = "memory"
    rate_limit_default: int = 60
    rate_limit_messages: int = 20
    rate_limit_runs: int = 5
    rate_limit_author: int = 3

    # LLM-assisted authoring (§6.3): the model tier the authoring endpoint uses.
    authoring_tier: str = "frontier"

    # Authoring sampling knobs (drafter creativity). The FIRST draft / refinement
    # generation samples at these values so persona NAMES + personality come out
    # distinctive and varied instead of bland-greedy. The validation-repair RETRY
    # stays deterministic (temperature 0.0) regardless of these — high temp to
    # invent, low temp to reliably fix schema errors (D-10-3 contract preserved).
    # Tunable without a redeploy via the env vars below.
    #   PERSONA_API_AUTHORING_TEMPERATURE — default 0.9 (creative but coherent).
    #   PERSONA_API_AUTHORING_TOP_P / _TOP_K — None ⇒ leave the provider default
    #   untouched. top_p is honoured by OpenAI + Anthropic; top_k only by
    #   providers that support it (Anthropic, hf_local, ollama) — it is a no-op on
    #   the OpenAI path. The production authoring tier is Anthropic, so top_k DOES
    #   reach the model.
    authoring_temperature: float = Field(default=0.9, ge=0.0, le=2.0)
    authoring_top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    authoring_top_k: int | None = Field(default=None, ge=1)

    # Issue 1 — build-time voice auto-assignment. The persona-voice service base
    # URL the create flow calls (``GET /v1/voices``, forwarding the caller's
    # bearer token) to pick a fitting voice from the language-filtered catalogue,
    # so a persona ships with a gender-appropriate voice instead of the global
    # English-male default. Empty disables the feature (personas keep the global
    # default). Read from ``PERSONA_VOICE_SERVICE_URL``.
    voice_service_url: str = Field(default="", validation_alias="PERSONA_VOICE_SERVICE_URL")
    # Model tier for the voice-pick reasoning (gender + character match). Small
    # is ample — it reads the persona identity + the compact catalogue.
    voice_pick_tier: str = "small"

    # Credits (D-08-6): flat per successful chat turn + per authoring call.
    credits_per_turn: int = 1
    authoring_credit_cost: int = 1000

    # CORS origins allowed to call the API from a browser (spec-09 web app).
    # Comma-separated; the web dev server is http://localhost:3000 by default.
    # Empty disables CORS (server-to-server only). Read from PERSONA_API_CORS_ORIGINS.
    cors_origins: str = "http://localhost:3000"

    # Spec 13 D-13-4: per-deployment workspace root for uploaded images (and
    # later per-persona tool artefacts). Each upload lands at
    # ``<workspace_root>/<owner_id>/<persona_id>/uploads/<digest><ext>`` and is
    # resolved via ``persona.tools._sandbox.resolve_sandbox_path``. Read from
    # ``PERSONA_API_WORKSPACE_ROOT``; defaults to a ``.persona_work`` dir under
    # the process cwd so a clean checkout runs without env setup.
    workspace_root: Path = Field(default_factory=lambda: Path.cwd() / ".persona_work")

    # Spec 29 D-29-3: wall-clock bound on build-time avatar auto-generation.
    # The hook in ``POST /v1/personas`` wraps ``imagegen.generate_avatar`` in
    # ``asyncio.wait_for(..., timeout=avatar_gen_timeout_s)`` so persona-create
    # latency stays bounded (NOT the imagegen provider's 120s ``request_timeout_s``
    # ceiling). On timeout the build fail-softs to ``avatar_url=null`` (D-29-X-
    # fail-soft). Read from ``PERSONA_API_AVATAR_GEN_TIMEOUT_S``.
    avatar_gen_timeout_s: float = Field(default=25.0, gt=0.0)

    @property
    def effective_app_database_url(self) -> str:
        """The DSN the request path connects with (RLS-enforced).

        Prefers ``app_database_url`` (the non-superuser ``persona_app`` role);
        falls back to ``database_url`` for single-role dev. Coerces a stray
        async DSN to the sync psycopg3 dialect (D-07-1).
        """
        url = self.app_database_url or self.database_url
        return url.replace("+asyncpg", "+psycopg")

    @property
    def jwt_algorithms_list(self) -> list[str]:
        """The allowed JWT algorithms as a list."""
        return [a.strip() for a in self.jwt_algorithms.split(",") if a.strip()]

    @property
    def cors_origins_list(self) -> list[str]:
        """The CORS-allowed origins as a list (empty disables CORS)."""
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def mcp_adopt_vetted_list(self) -> list[str]:
        """The cloud operator-vetted catalog names a persona may self-adopt (N4-D-6)."""
        return [n.strip() for n in self.mcp_adopt_vetted.split(",") if n.strip()]
