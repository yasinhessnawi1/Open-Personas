"""Persona schema — the v1.0 YAML model.

Mirrors architecture §4.2 and spec 01 §4. Every model is frozen + extra=forbid
so a typo in a persona YAML is caught at load time, not three layers deep
during a chat turn (fail-fast principle, ENGINEERING_STANDARDS.md §1.2).
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — Pydantic needs runtime access
from pathlib import Path  # noqa: TC003 — Pydantic needs runtime access
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from yaml import YAMLError

from persona.errors import PersonaError, PersonaNotFoundError, SchemaVersionMismatchError

__all__ = [
    "SUPPORTED_SCHEMA_VERSIONS",
    "CatalogueVoice",
    "EmbeddingConfig",
    "EpisodicEntry",
    "IntelligentRoutingConfig",
    "ModelScoringWeights",
    "Persona",
    "PersonaIdentity",
    "RoutingBudgetConfig",
    "RoutingConfig",
    "SelfFact",
    "VoiceSpec",
    "WorldviewClaim",
]

SUPPORTED_SCHEMA_VERSIONS: frozenset[str] = frozenset({"1.0"})


class CatalogueVoice(BaseModel):
    """A persona's voice, selected from a TTS provider's catalogue (Spec V3).

    The v1 member of the voice-resolution indirection
    (D-V3-X-cloning-seam-shape): a persona's ``voice`` resolves at synthesis
    time (in ``persona-voice``) to a provider voice. ``kind`` is the
    discriminator a v0.2 cloned-voice member extends the :data:`VoiceSpec`
    union by — the ``voice`` field annotation never changes, only the alias,
    so cloning slots in additively without re-architecting. ``consent`` is
    reserved (always ``None`` in v1); the cloning-consent record lands here
    in v0.2. Cloning itself is OUT of v1 (biometric-adjacent serious-harm
    surface requiring its own consent + safety design).

    Attributes:
        kind: Discriminator. Always ``"catalogue"`` in v1 (the default, so a
            YAML mapping may omit it).
        provider: The TTS provider whose catalogue this voice belongs to
            (e.g. ``"cartesia"``). Lowercase; validated against the
            configured backend at resolution time.
        voice_id: The provider-scoped voice handle (a catalogue voice id).
        consent: Reserved cloning-consent hook; always ``None`` at v1.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["catalogue"] = "catalogue"
    provider: str = Field(min_length=1)
    voice_id: str = Field(min_length=1)
    consent: None = None


# v1: a single-member alias. v0.2 promotes this to a discriminated union —
# ``Annotated[CatalogueVoice | ClonedVoice, Field(discriminator="kind")]`` —
# WITHOUT changing the ``PersonaIdentity.voice`` annotation (that is the whole
# point of the seam). Until then a plain alias avoids Pydantic's
# single-member-union edge case.
VoiceSpec = CatalogueVoice


class PersonaIdentity(BaseModel):
    """Who the persona is. Immutable at runtime; edit the YAML to change.

    Attributes:
        name: Display name (e.g., ``"Astrid"``).
        role: One-line role description.
        background: Multi-line background paragraph. Goes into the system
            prompt verbatim.
        language_default: ISO 639-1 code (``"en"``, ``"nb"``, ...).
        constraints: Hard constraints the persona must honour. Each entry
            is one constraint sentence.
        visual_style: Optional persona-level aesthetic descriptor consumed
            by the Spec 15 image-generation merge (D-15-4). Additive per
            D-01-12 / D-13-X-now — existing personas without the field are
            byte-for-byte unaffected. Merges at generation time, NOT at
            prompt-build time; the runtime prompt builder does not read it.
        voice: Optional per-persona voice — the audible analogue of
            ``visual_style`` (Spec V3, the F1 visual-identity sibling).
            A :class:`CatalogueVoice` resolved at synthesis time by
            ``persona-voice``; additive per D-01-12 — existing personas
            without it are byte-for-byte unaffected (criterion 4). May be
            authored in YAML as a mapping (``{provider: cartesia,
            voice_id: ...}``) or the shorthand string ``"cartesia:<id>"``,
            normalised at load. The runtime prompt builder does not read it.
        voice_by_provider: Optional per-provider voice memory (Spec V14-T5b,
            review finding I1's fix): maps a TTS provider name (e.g.
            ``"cartesia"``, ``"elevenlabs"``) to the ``voice_id`` this persona
            last used under that provider. Populated additively whenever
            ``voice`` is set — auto-pick, the provider-flip auto-remap, or a
            manual re-pick in the editor — so a later flip BACK to a provider
            this persona was already voiced under restores the exact same
            voice instead of a shared default or a fresh pick (lossless,
            bidirectional migration). Additive per D-01-12 — existing
            personas without it are byte-for-byte unaffected; ``None`` and
            ``{}`` are both "no memory yet". The runtime prompt builder does
            not read it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    role: str = Field(min_length=1)
    background: str = Field(min_length=1)
    language_default: str = "en"
    constraints: list[str] = Field(default_factory=list)
    visual_style: str | None = None
    voice: VoiceSpec | None = None
    voice_by_provider: dict[str, str] | None = None

    @field_validator("voice", mode="before")
    @classmethod
    def _normalise_voice(cls, value: object) -> object:
        """Accept the ``"provider:voice_id"`` shorthand for ``voice``.

        Parses the string form at the boundary into a
        :class:`CatalogueVoice` mapping (the ``kind`` defaults to
        ``"catalogue"``); mappings and instances pass through unchanged.
        Never passes a raw string inward (D-V3-X-voice-schema-shape).
        """
        if isinstance(value, str):
            provider, sep, voice_id = value.partition(":")
            if not sep or not provider.strip() or not voice_id.strip():
                raise ValueError(f"voice string must be 'provider:voice_id', got {value!r}")
            return {"provider": provider.strip(), "voice_id": voice_id.strip()}
        return value


class SelfFact(BaseModel):
    """A fact the persona holds about itself.

    Confidence is bounded to ``[0, 1]``. The store enforces the persona_self
    write threshold (D-01-5) against this value.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    fact: str = Field(min_length=1)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class WorldviewClaim(BaseModel):
    """A claim the persona holds about the world.

    The store enforces that persona_self writes always set ``epistemic`` —
    the default of ``"belief"`` covers author-time writes via the YAML.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    claim: str = Field(min_length=1)
    domain: str = ""
    epistemic: Literal["fact", "belief", "hypothesis", "contested"] = "belief"
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    valid_time: str = "always"


class EpisodicEntry(BaseModel):
    """A pre-loaded episodic memory (rare — episodic is usually runtime-written)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    content: str = Field(min_length=1)
    created_at: datetime
    importance: float = Field(default=0.5, ge=0.0, le=1.0)

    @field_validator("created_at", mode="after")
    @classmethod
    def _created_at_must_be_tz_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            msg = "naive datetime not allowed on EpisodicEntry.created_at"
            raise ValueError(msg)
        return value


class ModelScoringWeights(BaseModel):
    """Per-axis weights for the Spec 23 model-within-tier scorer (D-23-1).

    Weights need NOT sum to 1 — the scorer's weighted sum is relative, so any
    non-negative vector is valid (an all-zero vector defers entirely to the
    deterministic tie-break). Defaults mirror Spec 18's ``text_default`` profile
    (D-23-1): quality-led, cost second, latency light. Capability-fit is NOT a
    weight here — it is a hard pre-gate in the scorer
    (D-23-X-capability-filter-layering).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    cost: float = Field(default=0.40, ge=0.0)
    quality: float = Field(default=0.50, ge=0.0)
    latency: float = Field(default=0.10, ge=0.0)


class IntelligentRoutingConfig(BaseModel):
    """Opt-in metadata-driven model selection within a tier (Spec 23 §2.2; D-23-10).

    Additive + opt-in (D-23-9 / D-23-10): personas without a ``routing.intelligent``
    block load with ``enabled=False`` and route exactly as v0.1 (criterion 11).
    When ``enabled``, the :class:`IntelligentRouter` scores the chosen tier's
    candidate models and picks the best; on a metadata miss it degrades to the
    rule-based slot-0 selection when ``fallback_to_rule_based_on_miss`` (criterion
    9).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = False
    weights: ModelScoringWeights = Field(default_factory=ModelScoringWeights)
    fallback_to_rule_based_on_miss: bool = True


class RoutingBudgetConfig(BaseModel):
    """Optional per-persona spend caps (Spec 23 §2.4; D-23-7).

    Each cap is ``None`` by default (opt-in). The per-turn cap is HARD
    (:class:`~persona.backends.errors.BudgetExceededError` when no model fits);
    the per-session and per-day caps are SOFT (re-weight scoring toward cost as
    spend approaches them). The running session/day tally is owned by the runtime
    turn loop, not the schema.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_cents_per_turn: float | None = Field(default=None, ge=0.0)
    max_cents_per_session: float | None = Field(default=None, ge=0.0)
    max_cents_per_day: float | None = Field(default=None, ge=0.0)


class RoutingConfig(BaseModel):
    """Per-persona overrides for the runtime router (spec 05 + Spec 23).

    Spec 23 adds the additive optional ``intelligent`` and ``budget`` blocks
    (D-23-9: NO ``schema_version`` bump — the D-01-12 / ``visual_style`` /
    ``autonomy`` additive-optional precedent). Personas authored before Spec 23
    omit both and load byte-identically (criterion 11).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tier_for_generation: Literal["frontier", "mid", "small", "auto"] = "auto"
    tier_for_tools: Literal["frontier", "mid", "small", "auto"] = "small"
    intelligent: IntelligentRoutingConfig = Field(default_factory=IntelligentRoutingConfig)
    budget: RoutingBudgetConfig = Field(default_factory=RoutingBudgetConfig)
    #: Spec M1 — the user's chosen OpenRouter model id for THIS persona ("one model +
    #: auto-tier fallback"). ``None`` (the default, and the pre-M1 shape) ⇒ the tier
    #: system routes exactly as before; set ⇒ the loop prefers it when capable and
    #: falls back to the tier chain on error (additive-optional, the D-23-9 precedent —
    #: NO schema_version bump).
    preferred_model: str | None = Field(default=None, min_length=1)

    @field_validator("preferred_model")
    @classmethod
    def _preferred_model_not_blank(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            msg = "preferred_model must be a non-empty model id or None"
            raise ValueError(msg)
        return v


class EmbeddingConfig(BaseModel):
    """Embedder identity baked into the persona schema.

    Recording the embedder in the persona means a mismatch between the
    embedder used at index time and at query time is impossible by accident.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str = "bge-small-en-v1.5"
    dim: int = Field(default=384, gt=0)


class Persona(BaseModel):
    """A v1.0 persona document.

    See spec 01 §4.1. Constructed in two paths:
    - From a YAML file via :meth:`from_yaml` (the common case).
    - Directly via ``Persona(...)`` in tests.

    Attributes:
        persona_id: Stable identifier. Derived from the YAML filename's stem
            via :meth:`from_yaml` when absent from the document.
        schema_version: Must match a value in
            :data:`SUPPORTED_SCHEMA_VERSIONS`; older/newer versions raise
            :class:`SchemaVersionMismatchError` at load time.
        owner_id: Set at runtime by the hosted API; absent in author-time YAMLs.
        visibility: ``private`` for v0.1; ``unlisted``/``public`` are
            reserved for the post-September registry.
        identity: Required.
        self_facts: List of author-time self-facts. May be empty.
        worldview: List of author-time worldview claims. May be empty.
        episodic: Pre-loaded episodic memories. Empty in the common case
            because episodic grows at runtime.
        tools: Allow-list of tool names (spec 03 enforces).
        skills: List of declared skill pack names (spec 04 binds).
        routing: Per-persona router overrides.
        embedding: Embedder identity.
        autonomy: Author-time default for the proactive-autonomy preference
            (spec 21 §2, D-21-1). One of ``"cautious"`` (default — asks
            frequently, confirms more), ``"balanced"``, or ``"decisive"``
            (asks rarely, proceeds on assumptions). Additive per the D-01-12 /
            ``visual_style`` precedent: personas authored before spec 21 omit
            the field and load as ``"cautious"`` byte-for-byte unaffected.
            This is the *author-time default only* — the runtime-effective
            value is resolved at load time by overlaying any ``persona_self``
            self_facts head version under ``logical_id="autonomy"`` (D-21-8 /
            D-21-11). Autonomy is NOT identity: it is mutable at runtime via
            the versioned-append-only learner, unlike :class:`PersonaIdentity`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    persona_id: str | None = None
    schema_version: str = "1.0"
    owner_id: str | None = None
    visibility: Literal["private", "unlisted", "public"] = "private"

    identity: PersonaIdentity
    self_facts: list[SelfFact] = Field(default_factory=list)
    worldview: list[WorldviewClaim] = Field(default_factory=list)
    episodic: list[EpisodicEntry] = Field(default_factory=list)

    tools: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    autonomy: Literal["cautious", "balanced", "decisive"] = "cautious"

    @field_validator("schema_version", mode="after")
    @classmethod
    def _schema_version_supported(cls, value: str) -> str:
        if value not in SUPPORTED_SCHEMA_VERSIONS:
            supported = ", ".join(sorted(SUPPORTED_SCHEMA_VERSIONS))
            msg = (
                f"unsupported schema_version {value!r}; this build supports "
                f"{{{supported}}}. See migrations in docs/specs/ for upgrade paths."
            )
            raise ValueError(msg)
        return value

    @classmethod
    def from_yaml(cls, path: Path) -> Persona:
        """Load and validate a v1.0 persona YAML.

        ``persona_id`` is derived from ``path.stem`` when the YAML omits it.
        If the YAML declares its own ``persona_id``, the YAML wins (explicit
        beats implicit).

        Args:
            path: Path to a YAML file on disk.

        Returns:
            A validated, frozen :class:`Persona` instance.

        Raises:
            PersonaNotFoundError: ``path`` does not exist or is not a file.
            PersonaError: The file is not valid YAML.
            SchemaVersionMismatchError: ``schema_version`` is unsupported.
            pydantic.ValidationError: The YAML's shape does not match the
                schema. Bubbles up unchanged — Pydantic's error formatting
                is excellent and gives the user the path-into-the-document.
        """
        if not path.exists():
            raise PersonaNotFoundError(
                "persona YAML not found",
                context={"path": str(path)},
            )
        if not path.is_file():
            raise PersonaNotFoundError(
                "expected a file, not a directory",
                context={"path": str(path)},
            )
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except YAMLError as exc:
            raise PersonaError(
                "invalid YAML",
                context={"path": str(path), "reason": str(exc)[:120]},
            ) from exc
        if not isinstance(raw, dict):
            raise PersonaError(
                "persona YAML must be a mapping at the top level",
                context={"path": str(path), "actual_type": type(raw).__name__},
            )
        # Auto-derive persona_id from filename when absent. We do this *before*
        # construction because the model is frozen.
        if "persona_id" not in raw or raw["persona_id"] is None:
            raw["persona_id"] = path.stem
        try:
            return cls.model_validate(raw)
        except ValidationError as exc:
            # Special-case the schema_version mismatch so callers get a
            # specific domain exception instead of a generic ValidationError.
            for err in exc.errors():
                if err["loc"] == ("schema_version",) and "unsupported" in str(err.get("msg", "")):
                    raise SchemaVersionMismatchError(
                        str(err["msg"]),
                        context={"path": str(path)},
                    ) from exc
            raise
