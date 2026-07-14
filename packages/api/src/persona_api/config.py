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
from typing import TYPE_CHECKING

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from persona.skills.sources.github import GithubRepoSpec

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

    # Spec N6 (N6-D-5): the per-tenant MCP runtime runs a user's chosen image-MCP server
    # per tenant (a Fly Machine) with their secret injected — third-party code executed
    # per-tenant. Cloud must NOT do that silently: like the cloud-gateway guard, cloud
    # refuses to start with the per-tenant runtime configured (a Fly app set) unless the
    # operator explicitly acknowledges the vetted, per-active-tenant-cost posture here.
    # Ignored in community (which keeps N1's local gateway — no per-tenant Fly runtime).
    allow_per_tenant_mcp: bool = Field(
        default=False, validation_alias="PERSONA_ALLOW_PER_TENANT_MCP"
    )

    # Spec C6 (C6-D-0): the base URL of the separate connector service (C1-D-1) that the
    # web front-door proxies link-initiation to (``POST /v1/me/connectors/{platform}/link``
    # → ``{url}/v1/connectors/{platform}/link``, forwarding the caller's bearer). Empty ⇒
    # the front-door fails soft with a 503 "connector_unavailable" (never a dead spinner),
    # so a deploy without connectors configured degrades honestly. No trailing slash.
    connector_service_url: str = Field(default="", validation_alias="PERSONA_CONNECTOR_SERVICE_URL")

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

    # Spec K10 (D-K10-1/-5): how the community edition sources its store.
    #   ``legacy-sqlite`` — today's zero-infra SQLite + Chroma (DEPRECATED, D-K10-5;
    #     retained one release as the auto-import source + rollback target).
    #   ``external``      — use ``DATABASE_URL`` (a self-host power-user's Postgres).
    #   ``embedded``      — the bundled invisible Postgres 16 + pgvector.
    #   ``auto``          — detect a legacy SQLite store → auto-import it first
    #                       (T6/D-K10-4), then boot managed; absent → fresh embedded
    #                       (external ``DATABASE_URL`` when set, else the bundled PG).
    # The managed path (auto-import + full worker parity) is complete and validated
    # (T1–T8). The final one-line flip of this default from ``legacy-sqlite`` to
    # ``auto`` is GUARDED (D-K10-4/-5): it must NOT reach the owner's deployment until
    # the two real-data operator passes — a fresh managed install AND an
    # existing-install upgrade-with-import — are verified. Until then the default
    # stays ``legacy-sqlite`` so existing installs are byte-unchanged and no unit/CI
    # boot silently provisions Postgres; ``auto``/``embedded``/``external`` are opt-in.
    # Read from ``PERSONA_COMMUNITY_DB_MODE`` (no prefix — mirrors ``PERSONA_EDITION``).
    community_db_mode: str = Field(
        default="legacy-sqlite", validation_alias="PERSONA_COMMUNITY_DB_MODE"
    )

    # Spec K10 (D-K10-10): the base dir for the embedded managed-Postgres datadir.
    # The versioned datadir is ``<dir>/pg16`` — short so the unix-socket path stays
    # under the AF_UNIX cap. Read from ``PERSONA_API_COMMUNITY_MANAGED_DB_DIR``.
    community_managed_db_dir: Path = Field(default_factory=lambda: Path.home() / ".persona")

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
    # Spec R7 (R7-D-1/4/6) — denial-of-wallet caps. All three ride the edition seam:
    # ``cloud`` enforces, ``community`` no-ops (self-host is single-owner, no DoW
    # surface — the factory maps them to 0/unlimited). ``0`` = unlimited everywhere,
    # so an unset knob never regresses behaviour.
    #  * ``credits_max_per_day`` — per-user credits/UTC-day HARD spend cap (the primary
    #    DoW guard). Default 10,000 ≈ 10% of the 100,000 starter balance/day → a ~10-day
    #    floor for a normal user, a hard ceiling for an abuser.
    #  * ``max_concurrent_bounded_ops_per_user`` — N advisory slots for whole-op-in-one-txn
    #    ops (imagegen, voice). Default 1 preserves today's per-class cap exactly.
    #  * ``max_concurrent_long_ops_per_user`` — durable-count cap for long streams/jobs
    #    (chat SSE, agentic runs). Default 3 (the parallel-spend race the day-counter alone
    #    can't close — N long ops estimate up-front but book late).
    credits_max_per_day: int = Field(default=10_000, ge=0, validation_alias="CREDITS_MAX_PER_DAY")
    max_concurrent_bounded_ops_per_user: int = Field(
        default=1, ge=0, validation_alias="MAX_CONCURRENT_BOUNDED_OPS_PER_USER"
    )
    max_concurrent_long_ops_per_user: int = Field(
        default=3, ge=0, validation_alias="MAX_CONCURRENT_LONG_OPS_PER_USER"
    )
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
    # Spec A8 (A8-D-11) — the occurrences read API's server caps: a wide ?from&to can never
    # become unbounded engine iteration. Whichever binds first clamps the window/count and the
    # response carries an honest ``truncated`` marker (no silent cap).
    schedule_occurrences_max_horizon_days: int = Field(
        default=90, ge=1, validation_alias="PERSONA_SCHEDULE_OCCURRENCES_MAX_HORIZON_DAYS"
    )
    schedule_occurrences_max_count: int = Field(
        default=500, ge=1, validation_alias="PERSONA_SCHEDULE_OCCURRENCES_MAX_COUNT"
    )
    # R9-037 — the schedule-tombstone gate's lookback window: how long a user's
    # delete/edit of a schedule keeps refusing an autonomous seam's re-creation attempt
    # (e.g. the A5 initiative-scan-schedule ensure). A cool-down, not a permanent block —
    # the persona-level ``initiative_dial`` stays the authoritative permanent off-switch;
    # this only buys the user's just-expressed intent a month before the population-level
    # self-heal resumes. ``0`` disables gating entirely (an explicit escape hatch).
    schedule_tombstone_window_days: int = Field(
        default=30, ge=0, validation_alias="PERSONA_SCHEDULE_TOMBSTONE_WINDOW_DAYS"
    )
    # Spec A3 (T9/T13) — the two lifecycle sweeps hosted in the worker loop, each leader-gated
    # on its own advisory key. The approval sweep reminds a pending proposal at ~24h and
    # auto-expires (+ auto-pauses the task) at ~72h; the dead-leg sweep parks a retry-exhausted
    # task waiting(on_user) + voices an honest failure account. Both cadences are the outer poll
    # of a coarse, at-most-once operation, so a few minutes is ample (the CAS/idempotency is the
    # correctness spine, not the cadence). The remind/expire THRESHOLDS are the platform defaults
    # (24h/72h) overridable here.
    approval_sweep_interval_seconds: float = Field(
        default=300.0, gt=0, validation_alias="PERSONA_APPROVAL_SWEEP_INTERVAL_SECONDS"
    )
    approval_remind_after_hours: float = Field(
        default=24.0, gt=0, validation_alias="PERSONA_APPROVAL_REMIND_AFTER_HOURS"
    )
    approval_expire_after_hours: float = Field(
        default=72.0, gt=0, validation_alias="PERSONA_APPROVAL_EXPIRE_AFTER_HOURS"
    )
    dead_leg_sweep_interval_seconds: float = Field(
        default=120.0, gt=0, validation_alias="PERSONA_DEAD_LEG_SWEEP_INTERVAL_SECONDS"
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
    # R9-040 — the arbitrary-GitHub bring-your-own-repo skill source (S2 promised it; it had
    # zero production callers until this wiring). A comma list of `owner/repo[@ref]` entries
    # the sync clones + ingests via the EXISTING hardened D1 adapter
    # (persona.skills.sources.github) — each lands in the mirror at `third_party` (S2-D-3),
    # consent-gated downstream by S3 automatically, same as any other external skill. Stored
    # raw; parsed defensively (malformed entries WARN + skip, never fail config load — see
    # `skill_github_repos_parsed` / persona.skills.sources.github.parse_github_repo_specs).
    # Empty (default) -> no GitHub repos synced.
    skill_github_repos: str = Field(default="", validation_alias="PERSONA_SKILL_GITHUB_REPOS")
    # A0 T9 enqueue→worker cutover flag. OFF (default) → avatar generation runs the
    # legacy in-process BackgroundTasks path (contract unchanged). ON → the create
    # path ENQUEUES a durable avatar job for the worker (survives an api restart).
    # The orchestrator flips this at close-out once the worker is deployed.
    avatar_via_queue: bool = Field(default=False, validation_alias="PERSONA_API_AVATAR_VIA_QUEUE")

    # Spec K2 (T8d) — the synthesis-pipeline activation flags. The deploy is a
    # single uvicorn process (D-08-5), so the durable A0 worker + A1 scheduler tick
    # run as an IN-PROCESS background task started in the lifespan worker root
    # (``background.worker_root``), not a separate machine. When unset the effective
    # value is edition-derived (Spec K10 D-K10-7): ON for community-on-managed-Postgres
    # (the worker is what makes K2 synthesis / K7 consolidation / K8 gist REAL parity),
    # OFF otherwise (cloud/legacy keep the pre-activation posture — the orchestrator
    # flips it via env on the wired tier). An explicit ``PERSONA_API_IN_PROCESS_WORKER``
    # (``true``/``false``) always wins; ``None`` (unset) means "auto per edition". The
    # keyless fail-safe is preserved downstream: the worker still self-gates on a
    # ``tier_registry``, so a keyless boot never starts it (:func:`effective_in_process_worker`).
    in_process_worker: bool | None = Field(
        default=None, validation_alias="PERSONA_API_IN_PROCESS_WORKER"
    )
    # The tier the synthesis extractor + entity judge run on (D-K2-3). The hard
    # pre-live gate #2 re-runs the extraction corpus eval on THIS tier (NOT the
    # frontier/sonnet tier). ``small`` by default (cheap reflection pass).
    synthesis_tier: str = Field(default="small", validation_alias="PERSONA_API_SYNTHESIS_TIER")
    # Spec P9 (P9-D-2): the tier the A4/A8 intent interpreters (standing-intent /
    # amendment / steering / reschedule) run on. ``mid`` minimum — small was the R4
    # confabulation root (the recognizer never fired) and is measurably slower than
    # mid (P9-R-2). Override to ``frontier`` for reliability-critical deploys.
    recognition_tier: str = Field(default="mid", validation_alias="PERSONA_API_RECOGNITION_TIER")
    # R9-020: the tier conversation titles are generated on — the first-turn
    # auto-title AND the ``title_refresh`` background job. ``mid`` by default
    # (the "title" surface row): titles are user-read chrome, and small was the
    # measured echo→first-words-fallback root (the R4 sanitizer's fallback fired
    # on ~every turn). Override to ``frontier`` for maximum title quality.
    title_tier: str = Field(default="mid", validation_alias="PERSONA_API_TITLE_TIER")
    # R9-025b: the tier the "Turn into file" extraction pass runs on (mid — a
    # substance-extraction + format-decision call is worth the same quality bar
    # as titles, not the cheap background-summary tier). Mirrors title_tier's
    # own per-job-type tier-knob precedent (a dedicated field per background
    # job type: synthesis_tier / episodic_summary_tier / recognition_tier /
    # title_tier / this one — never a shared/reused knob across unrelated jobs).
    file_extract_tier: str = Field(default="mid", validation_alias="PERSONA_API_FILE_EXTRACT_TIER")
    # Spec P9 (P9-D-4/D-7): the GLOBAL gate for the Spec-23 intelligent
    # (model-within-tier) routing machinery. Default OFF — the deliberate
    # surface→tier policy is the path; the scorer is a dormant, reversible
    # lever. When off, the per-persona ``routing.intelligent.enabled`` flag is
    # never consulted (it is a web-form artifact, not a deliberate opt-in).
    # ⚠ Before enabling: repopulate the model-metadata tables for the deployed
    # models, or every pick silently degrades to rule-based slot-0.
    routing_intelligent_enabled: bool = Field(
        default=False, validation_alias="PERSONA_ROUTING_INTELLIGENT_ENABLED"
    )
    # Spec K8 (K8-D-10): the gist summarizer's OWN tier knob — independent of
    # synthesis so summarization cadence/cost is tunable on its own. The tier's
    # provider chain must be production-viable (the NIM-free primaries are
    # dev/test-ToS-bound — an env concern, flagged in the K8 research).
    episodic_summary_tier: str = Field(
        default="small", validation_alias="PERSONA_API_EPISODIC_SUMMARY_TIER"
    )
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

    # Spec N6 (N6-D-4): the operator allow-list of catalog images permitted to RUN
    # per-tenant (distinct from ``mcp_adopt_vetted``, which governs *remote* adoption).
    # Comma-list of catalog entry names. Cloud honors it as an allowlist on top of the
    # Docker-official ``mcp/`` namespace + provenance basis; the **empty default is
    # deny-all (fail-closed)** — nothing runs per-tenant until the operator vets it.
    # NOT signature-based (the mirror carries no image signatures, N6-R-1).
    mcp_run_vetted: str = Field(default="", validation_alias="PERSONA_MCP_RUN_VETTED")

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

    # Audit backend (R5-D-2). "jsonl" (default → the per-persona JSONL files;
    # community / single-node stays byte-unchanged) or "postgres" (the
    # multi-worker-safe store_audit_events / tool_audit_events tables). Selected
    # by _build_audit_logger / _build_tool_audit_logger (mirrors
    # rate_limit_backend). The JSONL logger's process-local threading.Lock is
    # NOT multi-worker-safe (S08-4) — set this to "postgres" before scaling the
    # API past one worker / one Machine.
    audit_backend: str = "jsonl"

    # Connection pool (research §5 — roomy pool removes store/CRUD contention).
    db_pool_size: int = 5

    # External file storage (R5-D-4). "local" (default → the workspace volume,
    # byte-unchanged) or "s3" (generic S3 — Tigris / R2 / MinIO / AWS via boto3).
    # S3 lifts the Fly-volume single-Machine pin so artifacts/uploads/images are
    # not host-pinned. boto3 is an OPTIONAL dep; only the s3 backend needs it.
    # Credentials come from the standard AWS_* env; bucket + endpoint + region are
    # here. Falls back to local when s3 is set but no bucket is configured.
    storage_backend: str = "local"
    storage_s3_bucket: str = ""
    # Tigris: "https://t3.storage.dev"; region "auto". Empty ⇒ boto3 default (AWS).
    storage_s3_endpoint_url: str = ""
    storage_s3_region: str = ""

    # Request telemetry (R5-D-3, §6.3 system-health). When true AND a platform
    # engine exists, RequestTelemetryMiddleware records one buffered, fail-soft
    # row per request into request_telemetry (off the request path). Default on;
    # a no-DB / no-engine process is a silent no-op.
    telemetry_enabled: bool = True

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

    # Credits (D-08-6): per successful chat turn + per authoring call.
    # Spec M2 (D-M2-5): ``credits_per_turn`` is now the FLOOR of the
    # proportional chat-turn charge (max(floor, ceil(cost_cents)) at
    # 1 credit = 1¢), not the whole price. Authoring stays flat.
    # Rollback fidelity (M2 re-verify, rider H2): kill-switch OFF
    # (PERSONA_API_PROPORTIONAL_CREDITS=false) + credits_per_turn > 1 restores
    # the full pre-M2 classic-reject semantics INCLUDING the balance < amount
    # NO-CHARGE arm (all-or-nothing reject — partial capture engages only on
    # basis-qualified proportional charges, never on the flat arm). Intended.
    credits_per_turn: int = 1
    # Spec M2 (D-M2-5, owner-approved rollback hatch): False reverts the chat
    # turn charge to the pre-M2 flat ``credits_per_turn`` (telemetry layers
    # unaffected). Read from PERSONA_API_PROPORTIONAL_CREDITS.
    proportional_credits: bool = True
    authoring_credit_cost: int = 1000

    # Spec M2 review (reviewer defense-in-depth, adjudicated TAKE): a hard
    # ceiling on the per-turn PROPORTIONAL charge computed in
    # ``ChatTurnRegistry._turn_charge``. A pricing/unit-scale bug (e.g. a
    # resolver returning $/Mtok where cents/1k-tokens was expected — a 10_000x
    # blowup) must never bill a single turn thousands of credits; the clamp
    # applies ONLY to the charged amount — the persisted TurnLog/UsageEntry
    # keeps the verbatim true cost (basis honesty is untouchable, M2 §2), and
    # a WARNING logs both numbers + the basis whenever it actually clamps.
    # ``<= 0`` disables the ceiling (unclamped — the pre-review shape). Read
    # from ``PERSONA_API_MAX_TURN_CREDITS``.
    max_turn_credits: int = 500

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

    def effective_in_process_worker(self, *, community_managed: bool) -> bool:
        """The effective in-process-worker switch (Spec K10 D-K10-7).

        An explicit ``PERSONA_API_IN_PROCESS_WORKER`` (``true``/``false``) always
        wins. When unset, default ON for community-on-managed-Postgres (the worker
        is what makes K2 synthesis / K7 consolidation / K8 gist actually run — real
        parity), OFF otherwise (cloud/legacy keep the pre-activation posture).

        This encodes ONLY the default-on policy; the keyless fail-safe is a separate
        downstream gate (the lifespan still requires a ``tier_registry``), so a
        keyless community boot never starts the worker even when this returns True.
        """
        if self.in_process_worker is not None:
            return self.in_process_worker
        return self.edition is Edition.community and community_managed

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

    @property
    def mcp_run_vetted_list(self) -> list[str]:
        """The cloud operator-vetted catalog names permitted to RUN per-tenant (N6-D-4)."""
        return [n.strip() for n in self.mcp_run_vetted.split(",") if n.strip()]

    @property
    def skill_github_repos_parsed(self) -> list[GithubRepoSpec]:
        """The configured arbitrary-GitHub BYO skill repos, parsed defensively (R9-040).

        Malformed entries are warned-and-skipped by the parser itself
        (:func:`~persona.skills.sources.github.parse_github_repo_specs`) — this property
        never raises on a bad entry (config load must not fail on an operator typo).
        """
        from persona.skills.sources.github import parse_github_repo_specs

        return parse_github_repo_specs(self.skill_github_repos)
