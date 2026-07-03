"""Settings for the persona-voice service (spec V1 T04).

All knobs land here via environment variables — twelve-factor discipline (per
the spec 08 ``APIConfig`` precedent). Secrets are :class:`pydantic.SecretStr`
so they never leak into ``repr()`` or logs by accident.

The JWT verifier fields (``jwt_secret`` / ``jwt_public_key`` / ``jwt_algorithms``
/ ``jwt_audience``) match the same names ``APIConfig`` exposes, so the same
``persona.auth.jwt_verifier.make_jwt_verifier`` (D-V1-X-jwt-verifier-extraction)
consumes either settings class via the structural ``JwtVerifierConfig`` Protocol.
This is the seam that lets persona-api and persona-voice share the auth surface
without persona-voice taking a persona-api dependency.
"""

from __future__ import annotations

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["VoiceConfig"]


class VoiceConfig(BaseSettings):
    """Environment-driven settings for the persona-voice service."""

    model_config = SettingsConfigDict(
        env_prefix="PERSONA_VOICE_",
        env_file=None,
        case_sensitive=False,
        extra="ignore",
        # So an explicit ``VoiceConfig(edition=...)`` kwarg (the field name) is
        # honored alongside the ``PERSONA_EDITION`` validation_alias (Spec 33).
        populate_by_name=True,
    )

    # --- Open-core edition (Spec 33, D-33-X-voice-edition) ---
    # Reads the SAME ``PERSONA_EDITION`` var as persona-api / persona-web (no
    # prefix). ``community`` (default): no-auth local voice — no JWT, a fixed
    # local owner, no credit metering. ``cloud``: verify the Clerk JWT + persona
    # ownership + credits (today's behavior, unchanged). persona-voice is MIT and
    # cannot import persona-api's ``Edition`` enum (the import-direction contract
    # forbids it), so the flag lives here as a plain string.
    edition: str = Field(default="community", validation_alias="PERSONA_EDITION")
    community_owner_id: str = Field(default="local-owner")
    community_owner_email: str = Field(default="local@localhost")

    @property
    def is_cloud(self) -> bool:
        """Whether this process runs the commercial cloud edition."""
        return self.edition.strip().lower() == "cloud"

    # --- LiveKit substrate (D-V1-1 branch (A), D-V1-X-livekit-server-deployment) ---
    # `LIVEKIT_URL` is the WebSocket URL the client uses to connect to the
    # LiveKit Server (ws:// for self-hosted; wss:// for production). The
    # server-side AccessToken JWTs are signed with `LIVEKIT_API_SECRET` and
    # consumed by the same self-hosted LiveKit Server, so the secret never
    # leaves the deployment.
    livekit_url: str = Field(default="ws://localhost:7880")
    livekit_api_key: SecretStr = Field(default=SecretStr(""))
    livekit_api_secret: SecretStr = Field(default=SecretStr(""))
    # Access-token TTL. The LiveKit Server re-checks expiry on every connection
    # event — including a reconnect/resume after a network blip — so the token
    # must outlive the WHOLE call, not just the join. 10 min was too short: a
    # call that ran (or a tab that resumed) past it failed to reconnect with
    # "token is expired". Default to 1h to cover a real conversation while
    # staying room-scoped + bounded; a deployment can raise/lower it via
    # ``PERSONA_VOICE_LIVEKIT_TOKEN_TTL_S``. (For arbitrarily long calls the full
    # fix is client-side token refresh; this default covers realistic lengths.)
    livekit_token_ttl_s: int = Field(default=3600)

    # --- JWT verification (matches the JwtVerifierConfig Protocol shape) ---
    # Identical surface to `APIConfig`'s JWT fields so the same
    # `make_jwt_verifier` from persona-core consumes either via structural
    # typing (D-V1-X-jwt-verifier-extraction).
    jwt_secret: SecretStr | None = Field(default=None)
    jwt_public_key: SecretStr | None = Field(default=None)
    jwt_algorithms: str = Field(default="HS256")
    jwt_audience: str | None = Field(default=None)

    # --- Database (RLS-scoped persona-core direct access per D-V1-4) ---
    # Same persona_app non-superuser role as persona-api (D-07-5); RLS scopes
    # every connection via the request-scoped contextvar.
    database_url: str = Field(default="")

    # --- CORS (browser → voice service is cross-origin, like persona-api) ---
    # The web app (default :3000) calls POST /v1/voice/token + GET /v1/voices
    # directly from the browser. Bearer auth (no cookies). Empty disables CORS.
    # Read from PERSONA_VOICE_CORS_ORIGINS (comma-separated).
    cors_origins: str = Field(default="http://localhost:3000")

    # --- Dev agent worker (spec V6 A0, D-V6-X-agent-worker) ---
    # When true, ``POST /v1/voice/token`` ALSO launches an in-process agent
    # session that joins the call's Room and becomes the persona on the call
    # (the dev/operator-pass-grade composition-root runner). Default FALSE —
    # production worker-ops are a separate forward-item, and existing token-only
    # deployments + tests are unaffected. Requires ``database_url`` +
    # ``livekit_api_*`` + the provider keys (PERSONA_STT_*/PERSONA_TTS_*/tiers).
    agent_inprocess: bool = Field(default=False)

    # --- Voice graph memory (Spec V13, V13-D-6 kill-switch) ---
    # Gates whether the K4-gated graph store is composed into the voice runner.
    # ``False`` (default): the runner composes no graph store ⇒ ``VoiceTurnContext``
    # keeps ``graph_retrieval``/``graph_surfacing_guidance`` ``None`` ⇒ the byte-
    # identical graph-OFF turn (today's behaviour). ``True``: compose the graph
    # store + the K4-gated retrieval (allowlist subtraction + recent-window lift +
    # surfacing + recency, mirrored from chat). Ship OFF; flip ON in-env only after
    # the R4 operator pass ratifies latency (by ear) + spoken care. This switch
    # governs the READ/surfacing path ONLY — episodic parity + accumulation carry no
    # spoken-surfacing risk and are not gated by it.
    graph_memory_enabled: bool = Field(default=False)

    # --- Greet-first turn-0 bounds (Spec 32 A3, D-32-X-degrade-timeout-env-config) ---
    # The ring degrade ladder, env-tunable per the config-via-env standard.
    # ``greet_warmup_timeout_s`` caps how long turn 0 waits on the embedder warm-up
    # before proceeding (the ring covers the rest); ``greet_timeout_s`` caps how
    # long the call may ring with no greeting audio before degrading to the user's
    # floor (never ring forever, D-32-3). Defaults are the researched ladder.
    greet_warmup_timeout_s: float = Field(default=10.0, gt=0.0)
    greet_timeout_s: float = Field(default=30.0, gt=0.0)

    @field_validator("jwt_algorithms", mode="before")
    @classmethod
    def _normalise_algorithms(cls, v: object) -> str:
        """Allow ``PERSONA_VOICE_JWT_ALGORITHMS=HS256,RS256`` env-var form."""
        if v is None:
            return "HS256"
        return str(v)

    @property
    def jwt_algorithms_list(self) -> list[str]:
        """Computed list form consumed by ``make_jwt_verifier``."""
        return [a.strip() for a in self.jwt_algorithms.split(",") if a.strip()]

    @property
    def cors_origins_list(self) -> list[str]:
        """The CORS-allowed origins as a list (empty disables CORS)."""
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]
