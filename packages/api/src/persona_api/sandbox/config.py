"""Sandbox-pool configuration via env vars (spec 12 T09b; locked by D-12-17).

Read once at process start through ``pydantic-settings``; the composition root
constructs a :class:`SandboxPoolConfig` and passes the values into
:class:`persona_api.sandbox.pool.SandboxPool`. Aligns with the project rule
"Config via env vars + Pydantic Settings only" (CLAUDE.md).

The ``PERSONA_SANDBOX_`` prefix matches the existing ``PERSONA_SANDBOX_IMAGE``
env var documented in ``.env.example`` — sandbox-scope config, distinct from
the API-scope ``PERSONA_API_*`` knobs in :class:`persona_api.config.APIConfig`.

**D-12-17 locked defaults:**

  - ``warm_pool_size`` = 0 — no idle slots; first acquire pays the substrate
    cold-start (~2.305s p95 per Gate 1) within the acquire call. Nonzero
    values require the warm-pool maintainer (deferred to a future spec when
    telemetry triggers the flip; see D-12-17 flip-triggers).
  - ``reap_interval_s`` = 60 — background reaper sweep cadence.
  - ``idle_timeout_s`` = 300 — session staleness before reap.
  - ``max_per_user`` = 2 — per-tenant concurrent-session cap; bounds the
    multi-tenant attack surface (SCP-12-1 reference).
"""

from __future__ import annotations

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["SandboxPoolConfig", "SandboxTemplateConfig", "SandboxWallClockConfig"]


class SandboxWallClockConfig(BaseSettings):
    """Env-driven wall-clock dual policy for code execution (Spec 25 D-25-2/3).

    Acceptance criterion 2: env-setup commands (package-manager invocations,
    per :func:`persona_api.sandbox.hosted.detect_env_setup`) get a longer
    wall-clock cap than ordinary code so a one-off ``pip install`` isn't
    killed at the 30s exec budget, while runaway compute still is.

    Both caps are tunable via env (folds in the Spec 25 kickoff's
    ``D-25-X-cap-env-overrides``):

      - ``PERSONA_SANDBOX_WALLCLOCK_EXEC_S`` — ordinary code-execution cap
        (D-25-3 default: 30s; unchanged from Spec 12).
      - ``PERSONA_SANDBOX_WALLCLOCK_SETUP_S`` — env-setup cap (D-25-3
        default: 120s).

    Read once at process start by the composition root and passed into
    :class:`persona_api.sandbox.hosted.HostedSandbox`. Shares the
    ``PERSONA_SANDBOX_`` prefix with :class:`SandboxPoolConfig`; the env-var
    names use explicit aliases so they read ``..._WALLCLOCK_EXEC_S`` rather
    than the field-derived ``..._EXEC_CAP_S``.
    """

    model_config = SettingsConfigDict(
        env_prefix="PERSONA_SANDBOX_", extra="ignore", populate_by_name=True
    )

    exec_cap_s: float = Field(
        default=30.0,
        validation_alias=AliasChoices("exec_cap_s", "PERSONA_SANDBOX_WALLCLOCK_EXEC_S"),
        description=(
            "Wall-clock cap (seconds) for ordinary code execution. D-25-3 "
            "default: 30s (unchanged from Spec 12)."
        ),
    )
    setup_cap_s: float = Field(
        default=120.0,
        validation_alias=AliasChoices("setup_cap_s", "PERSONA_SANDBOX_WALLCLOCK_SETUP_S"),
        description=(
            "Wall-clock cap (seconds) for env-setup commands (package-manager "
            "invocations per D-25-2). D-25-3 default: 120s."
        ),
    )

    @field_validator("exec_cap_s", "setup_cap_s")
    @classmethod
    def _positive_cap_seconds(cls, v: float) -> float:
        if v <= 0:
            msg = f"wall-clock cap must be > 0; got {v}"
            raise ValueError(msg)
        return v


class SandboxTemplateConfig(BaseSettings):
    """The custom-template selection for the hosted sandbox (Spec P5, P5-D-3).

    ``PERSONA_SANDBOX_TEMPLATE`` (sandbox-scope, beside the pool/wall-clock config)
    names the **E2B template alias** to run — the owner's custom
    ``persona-docgen-interpreter`` (reportlab + python-pptx baked in, egress still
    off, D-12-4). **Unset ⇒ ``None`` ⇒ the SDK default ``code-interpreter-v1``**, so
    community/OSS deploys without the custom template keep working unchanged
    (P5-D-6). The alias (not an immutable build id) is stored so the owner can
    rebuild the template for a lib security patch without changing the secret.

    ``docgen_full_fidelity`` is the **single source of truth** (P5-D-4) derived from
    this one value: a custom template being configured means the doc-gen libs are
    present, so ``document_generation`` may produce ``.pdf`` via reportlab and offer
    ``.pptx`` via python-pptx up-front. Unset ⇒ the spec-A offline-degrade fallback.
    The composition root computes this and hands it to the (core-side) skill
    assembly; **persona-core never reads ``PERSONA_SANDBOX_*``** — the api→core
    decoupling guard holds. A template that is set but lacks the libs is covered by
    the skill's runtime try-import belt-and-braces (P5-D-4), not by this flag.
    """

    model_config = SettingsConfigDict(
        env_prefix="PERSONA_SANDBOX_", extra="ignore", populate_by_name=True
    )

    template: str | None = Field(
        default=None,
        description=(
            "E2B sandbox template alias (PERSONA_SANDBOX_TEMPLATE). None ⇒ the SDK "
            "default code-interpreter-v1 (community/OSS path). Set to the owner's "
            "custom doc-gen template alias in cloud deploys."
        ),
    )

    @field_validator("template")
    @classmethod
    def _blank_is_unset(cls, v: str | None) -> str | None:
        """Treat an empty / whitespace-only value as unset (a blank Fly secret must
        degrade to the SDK default, never select an empty template name)."""
        if v is None:
            return None
        stripped = v.strip()
        return stripped or None

    @property
    def docgen_full_fidelity(self) -> bool:
        """True ⇒ a custom template is configured ⇒ full pdf/pptx fidelity (P5-D-4)."""
        return self.template is not None


class SandboxPoolConfig(BaseSettings):
    """Environment-driven configuration for :class:`SandboxPool` (D-12-17)."""

    model_config = SettingsConfigDict(
        env_prefix="PERSONA_SANDBOX_", extra="ignore", populate_by_name=True
    )

    warm_pool_size: int = Field(
        default=0,
        description=(
            "Idle slots maintained per (user_id, conversation_id). v0.1 locks to 0 "
            "(D-12-17). Nonzero requires a maintainer task that v0.1 does not ship; "
            "production sets this only after the D-12-17 warm-pool flip-trigger fires."
        ),
    )
    reap_interval_s: float = Field(
        default=60.0,
        description="Background reaper sweep cadence. D-12-17 default: 60s.",
    )
    idle_timeout_s: float = Field(
        default=300.0,
        description=(
            "Seconds without activity before a session is reaped. D-12-17 default: "
            "300s (matches HostedSandbox.timeout_default_s by intent)."
        ),
    )
    max_per_user: int = Field(
        default=2,
        description=(
            "Per-user concurrent-session cap. D-12-17 default: 2. Bounds "
            "multi-tenant attack surface (SCP-12-1); per-VM properties don't "
            "compound across slots."
        ),
    )

    @field_validator("warm_pool_size")
    @classmethod
    def _v01_locks_warm_pool_to_zero(cls, v: int) -> int:
        """D-12-17 v0.1 lock: nonzero warm-pool size requires the maintainer task.

        The maintainer code does not ship in v0.1 (T09b deliberately scopes to
        the reaper only). Production flips this to a positive value only after
        the D-12-17 telemetry-driven flip-trigger fires AND the maintainer
        spec lands. Surfacing the gap explicitly here keeps the env var
        forward-compatible without silently accepting a value the pool
        can't honour.
        """
        if v < 0:
            msg = f"warm_pool_size must be >= 0; got {v}"
            raise ValueError(msg)
        if v > 0:
            msg = (
                f"warm_pool_size={v} requires the warm-pool maintainer task, which "
                "v0.1 does not ship (D-12-17 locks the v0.1 value to 0; nonzero "
                "values land in a follow-up spec when production telemetry "
                "triggers the warm-pool flip)."
            )
            raise ValueError(msg)
        return v

    @field_validator("reap_interval_s", "idle_timeout_s")
    @classmethod
    def _positive_seconds(cls, v: float) -> float:
        if v <= 0:
            msg = f"value must be > 0; got {v}"
            raise ValueError(msg)
        return v

    @field_validator("max_per_user")
    @classmethod
    def _positive_cap(cls, v: int) -> int:
        if v < 1:
            msg = f"max_per_user must be >= 1; got {v}"
            raise ValueError(msg)
        return v
