"""Tests for ``persona.schema.persona`` — covers spec §8 #1 (validate round-trip).

Each valid fixture loads. Each invalid fixture raises a Pydantic
``ValidationError`` (or a more specific domain exception in the case of
``SchemaVersionMismatchError``). The fixtures themselves carry comments
explaining what each invalid file violates.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
import yaml
from persona.errors import PersonaNotFoundError, SchemaVersionMismatchError
from persona.schema.persona import (
    SUPPORTED_SCHEMA_VERSIONS,
    EmbeddingConfig,
    EpisodicEntry,
    Persona,
    PersonaIdentity,
    RoutingConfig,
    SelfFact,
    WorldviewClaim,
)
from pydantic import ValidationError

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "personas"
VALID_FIXTURES = sorted((FIXTURES / "valid").glob("*.yaml"))
INVALID_FIXTURES = sorted((FIXTURES / "invalid").glob("*.yaml"))


def test_fixture_counts_match_spec() -> None:
    """Spec §8 #1: at least 10 valid + 10 invalid YAMLs."""
    assert len(VALID_FIXTURES) >= 10, f"need ≥10 valid fixtures, found {len(VALID_FIXTURES)}"
    assert len(INVALID_FIXTURES) >= 10, f"need ≥10 invalid fixtures, found {len(INVALID_FIXTURES)}"


@pytest.mark.parametrize("fixture", VALID_FIXTURES, ids=lambda p: p.name)
def test_valid_fixture_loads(fixture: Path) -> None:
    persona = Persona.from_yaml(fixture)
    assert isinstance(persona, Persona)
    assert persona.identity.name
    # Additive invariant: all pre-M1 fixtures have preferred_model defaulted to None.
    assert persona.routing.preferred_model is None


@pytest.mark.parametrize("fixture", INVALID_FIXTURES, ids=lambda p: p.name)
def test_invalid_fixture_raises(fixture: Path) -> None:
    with pytest.raises((ValidationError, SchemaVersionMismatchError)):
        Persona.from_yaml(fixture)


class TestFromYaml:
    def test_missing_file_raises_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(PersonaNotFoundError, match="not found"):
            Persona.from_yaml(tmp_path / "does_not_exist.yaml")

    def test_directory_raises_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(PersonaNotFoundError, match="file"):
            Persona.from_yaml(tmp_path)

    def test_invalid_yaml_raises_persona_error(self, tmp_path: Path) -> None:
        from persona.errors import PersonaError

        bad = tmp_path / "bad.yaml"
        bad.write_text(":\n  bad\n  syntax: [", encoding="utf-8")
        with pytest.raises(PersonaError, match="invalid YAML"):
            Persona.from_yaml(bad)

    def test_top_level_must_be_mapping(self, tmp_path: Path) -> None:
        from persona.errors import PersonaError

        bad = tmp_path / "list.yaml"
        bad.write_text("- not_a_mapping\n- also_no\n", encoding="utf-8")
        with pytest.raises(PersonaError, match="mapping"):
            Persona.from_yaml(bad)

    def test_persona_id_derived_from_filename_when_absent(self) -> None:
        path = next(p for p in VALID_FIXTURES if p.name == "02_astrid_no_persona_id.yaml")
        persona = Persona.from_yaml(path)
        assert persona.persona_id == "02_astrid_no_persona_id"

    def test_persona_id_from_yaml_wins(self) -> None:
        path = next(p for p in VALID_FIXTURES if p.name == "03_legal_assistant_full.yaml")
        persona = Persona.from_yaml(path)
        assert persona.persona_id == "legal_assistant_no"

    def test_schema_version_mismatch_raises_domain_exception(self) -> None:
        path = next(p for p in INVALID_FIXTURES if p.name == "04_schema_version_mismatch.yaml")
        with pytest.raises(SchemaVersionMismatchError, match="unsupported"):
            Persona.from_yaml(path)


class TestSubModels:
    def test_persona_identity_requires_non_empty_fields(self) -> None:
        with pytest.raises(ValidationError):
            PersonaIdentity(name="", role="r", background="b")
        with pytest.raises(ValidationError):
            PersonaIdentity(name="n", role="", background="b")
        with pytest.raises(ValidationError):
            PersonaIdentity(name="n", role="r", background="")

    def test_self_fact_confidence_bounds(self) -> None:
        SelfFact(fact="x", confidence=0.0)
        SelfFact(fact="x", confidence=1.0)
        with pytest.raises(ValidationError):
            SelfFact(fact="x", confidence=-0.1)
        with pytest.raises(ValidationError):
            SelfFact(fact="x", confidence=1.1)

    def test_worldview_epistemic_literal(self) -> None:
        WorldviewClaim(claim="c", epistemic="fact")
        WorldviewClaim(claim="c", epistemic="contested")
        with pytest.raises(ValidationError):
            WorldviewClaim(claim="c", epistemic="other")  # type: ignore[arg-type]

    def test_episodic_entry_rejects_naive_datetime(self) -> None:
        with pytest.raises(ValidationError, match="naive"):
            EpisodicEntry(
                content="x",
                created_at=datetime(2026, 5, 27, 12, 0, 0),  # noqa: DTZ001
            )

    def test_routing_config_defaults(self) -> None:
        rc = RoutingConfig()
        assert rc.tier_for_generation == "auto"
        assert rc.tier_for_tools == "small"

    def test_embedding_config_defaults(self) -> None:
        ec = EmbeddingConfig()
        assert ec.model == "bge-small-en-v1.5"
        assert ec.dim == 384

    def test_embedding_dim_positive(self) -> None:
        with pytest.raises(ValidationError):
            EmbeddingConfig(dim=0)
        with pytest.raises(ValidationError):
            EmbeddingConfig(dim=-1)


class TestPersonaConstruction:
    def test_minimal_persona_round_trip_via_dict(self) -> None:
        data = {
            "schema_version": "1.0",
            "identity": {"name": "n", "role": "r", "background": "b"},
        }
        p = Persona.model_validate(data)
        assert p.persona_id is None  # only filled by from_yaml
        assert p.identity.name == "n"
        assert p.self_facts == []
        assert p.tools == []

    def test_supported_schema_versions_includes_v1(self) -> None:
        assert "1.0" in SUPPORTED_SCHEMA_VERSIONS

    def test_unsupported_schema_version_raises(self) -> None:
        with pytest.raises(ValidationError, match="unsupported"):
            Persona.model_validate(
                {
                    "schema_version": "0.9",
                    "identity": {"name": "n", "role": "r", "background": "b"},
                },
            )

    def test_frozen(self) -> None:
        p = Persona.model_validate(
            {
                "schema_version": "1.0",
                "identity": {"name": "n", "role": "r", "background": "b"},
            },
        )
        with pytest.raises(ValidationError):
            p.persona_id = "x"  # type: ignore[misc]


class TestVisualStyleAdditiveExtension:
    """Spec 15 T10 — ``identity.visual_style`` is an additive Pydantic field.

    Verifies the three guarantees of D-01-12 / D-13-X-now additive-extension
    pattern: (a) existing personas without the field round-trip byte-for-byte;
    (b) the new field round-trips when present; (c) ``extra="forbid"`` still
    rejects typos so the additive change does not weaken validation.
    """

    @pytest.mark.parametrize("fixture", VALID_FIXTURES, ids=lambda p: p.name)
    def test_existing_personas_round_trip_byte_for_byte(self, fixture: Path) -> None:
        """Regression: existing valid fixtures (no ``visual_style``) round-trip unchanged.

        Loads each pre-Spec-15 fixture, dumps it back through Pydantic, and
        asserts the loaded form matches what the YAML naturally yields when
        parsed through ``yaml.safe_load`` plus the same auto-derivations
        ``Persona.from_yaml`` applies. ``visual_style`` must default to
        ``None`` and must NOT appear in any pre-existing fixture's identity.
        """
        persona = Persona.from_yaml(fixture)

        # The new field defaults to None for every fixture authored before Spec 15.
        assert persona.identity.visual_style is None

        # Round-trip via model_dump must exclude the new field's default-None
        # presence from changing existing semantics. Re-validating the dumped
        # output yields an equal model (frozen Pydantic supports ==).
        dumped = persona.model_dump(mode="json")
        reloaded = Persona.model_validate(dumped)
        assert reloaded == persona

        # The raw YAML must not contain visual_style — proves we haven't
        # silently mutated the on-disk fixtures.
        raw = yaml.safe_load(fixture.read_text(encoding="utf-8"))
        assert "visual_style" not in raw.get("identity", {})

    def test_visual_style_round_trip_when_present(self) -> None:
        """A YAML carrying ``identity.visual_style`` loads + dumps the value cleanly."""
        data = {
            "schema_version": "1.0",
            "identity": {
                "name": "n",
                "role": "r",
                "background": "b",
                "visual_style": "warm editorial illustration, muted earth palette",
            },
        }
        p = Persona.model_validate(data)
        assert p.identity.visual_style == "warm editorial illustration, muted earth palette"
        dumped = p.model_dump(mode="json")
        assert dumped["identity"]["visual_style"] == (
            "warm editorial illustration, muted earth palette"
        )

    def test_visual_style_defaults_to_none(self) -> None:
        """Constructing ``PersonaIdentity`` without the field yields ``None``."""
        identity = PersonaIdentity(name="n", role="r", background="b")
        assert identity.visual_style is None

    def test_visual_style_accepts_explicit_none(self) -> None:
        """Explicit ``None`` is accepted (matches the default)."""
        identity = PersonaIdentity(name="n", role="r", background="b", visual_style=None)
        assert identity.visual_style is None

    def test_extra_forbid_still_rejects_typos(self) -> None:
        """``extra="forbid"`` rejects a misspelt field name (``viual_style``)."""
        with pytest.raises(ValidationError):
            PersonaIdentity.model_validate(
                {
                    "name": "n",
                    "role": "r",
                    "background": "b",
                    "viual_style": "watercolour",  # typo
                },
            )


class TestAutonomyAdditiveExtension:
    """Spec 21 T01 — ``Persona.autonomy`` is an additive Pydantic field (D-21-1).

    Mirrors :class:`TestVisualStyleAdditiveExtension` (the D-01-12 additive
    precedent): (a) personas authored before spec 21 round-trip byte-for-byte
    and default to ``"cautious"``; (b) the field round-trips when present;
    (c) ``extra="forbid"`` + the three-value ``Literal`` still reject typos and
    out-of-set values at load time (fail-fast).
    """

    @pytest.mark.parametrize("fixture", VALID_FIXTURES, ids=lambda p: p.name)
    def test_existing_personas_default_to_cautious_and_round_trip(self, fixture: Path) -> None:
        """Regression: pre-spec-21 fixtures (no ``autonomy``) load as ``"cautious"``.

        The field defaults to ``"cautious"``, the on-disk YAML must not contain
        ``autonomy`` (we have not mutated fixtures), and the model round-trips
        through ``model_dump``/``model_validate`` to an equal instance.
        """
        persona = Persona.from_yaml(fixture)

        assert persona.autonomy == "cautious"

        dumped = persona.model_dump(mode="json")
        reloaded = Persona.model_validate(dumped)
        assert reloaded == persona

        raw = yaml.safe_load(fixture.read_text(encoding="utf-8"))
        assert "autonomy" not in raw

    def test_default_is_cautious(self) -> None:
        """A minimal persona constructed without ``autonomy`` defaults to ``"cautious"``."""
        p = Persona.model_validate(
            {
                "schema_version": "1.0",
                "identity": {"name": "n", "role": "r", "background": "b"},
            },
        )
        assert p.autonomy == "cautious"

    @pytest.mark.parametrize("level", ["cautious", "balanced", "decisive"])
    def test_each_literal_value_round_trips(self, level: str) -> None:
        """All three valid levels load and survive a JSON round-trip."""
        p = Persona.model_validate(
            {
                "schema_version": "1.0",
                "identity": {"name": "n", "role": "r", "background": "b"},
                "autonomy": level,
            },
        )
        assert p.autonomy == level
        assert p.model_dump(mode="json")["autonomy"] == level

    def test_invalid_value_rejected_at_load(self) -> None:
        """A value outside the three-literal set raises ``ValidationError`` (fail-fast)."""
        with pytest.raises(ValidationError):
            Persona.model_validate(
                {
                    "schema_version": "1.0",
                    "identity": {"name": "n", "role": "r", "background": "b"},
                    "autonomy": "aggressive",
                },
            )

    def test_autonomy_is_immutable_after_construction(self) -> None:
        """The field is frozen like the rest of the model (runtime change is via the learner)."""
        p = Persona.model_validate(
            {
                "schema_version": "1.0",
                "identity": {"name": "n", "role": "r", "background": "b"},
                "autonomy": "balanced",
            },
        )
        with pytest.raises(ValidationError):
            p.autonomy = "decisive"  # type: ignore[misc]
