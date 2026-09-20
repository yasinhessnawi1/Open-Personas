"""Unit tests for the additive ``presentation`` identity field (R9-155, T1).

``presentation`` is the one AUTHORED answer to "how does this persona show up",
read later by both the avatar prompt crafter and the voice picker so the two can
no longer decide it independently and contradict each other.

What these tests pin:

* **Additivity.** A persona authored before the field existed loads with
  ``presentation is None``, and ``None`` stays a third state meaning "not
  stated" rather than a silent default of human. A default here would be the
  guess the field exists to remove, so it is asserted, not assumed.
* **The closed sets.** ``form`` and ``presents`` reject anything outside their
  declared values, while tolerating the case and whitespace a language model
  will produce.
* **``appearance`` is human-only and never blank**, because a synthetic persona
  has no portrait for it to describe and a whitespace-only descriptor declares
  nothing while looking declared.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from persona.schema.persona import Persona, PersonaIdentity, PersonaPresentation
from pydantic import ValidationError

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "personas"
VALID_FIXTURES = sorted((FIXTURES / "valid").glob("*.yaml"))


def _identity(**overrides: object) -> PersonaIdentity:
    base: dict[str, object] = {
        "name": "Astrid",
        "role": "guide",
        "background": "A calm narrator.",
    }
    base.update(overrides)
    return PersonaIdentity(**base)  # type: ignore[arg-type]


# ---------- additivity (the D-01-12 / visual_style / voice precedent) ------


def test_presentation_defaults_to_none() -> None:
    # Every persona already on disk and in the database omits this field; they
    # must keep loading, and they must NOT be quietly labelled human.
    assert _identity().presentation is None


def test_existing_identity_fields_unchanged() -> None:
    identity = _identity()
    assert identity.name == "Astrid"
    assert identity.visual_style is None
    assert identity.voice is None


def test_identity_accepts_presentation_as_a_mapping() -> None:
    identity = _identity(presentation={"form": "human", "presents": "masculine"})
    assert identity.presentation == PersonaPresentation(form="human", presents="masculine")


def test_presentation_round_trips_through_json() -> None:
    identity = _identity(
        presentation={"form": "human", "presents": "feminine", "appearance": "a woman in her 40s"}
    )
    restored = PersonaIdentity.model_validate_json(identity.model_dump_json())
    assert restored.presentation == identity.presentation


def test_identity_is_still_frozen_with_the_new_field() -> None:
    identity = _identity(presentation={"form": "synthetic"})
    with pytest.raises(ValidationError):
        identity.presentation = None  # type: ignore[misc]


# ---------- the loader (adding an optional field is not a version event) ----


@pytest.mark.parametrize("fixture", VALID_FIXTURES, ids=lambda p: p.name)
def test_every_persona_on_disk_still_loads_with_no_presentation(fixture: Path) -> None:
    persona = Persona.from_yaml(fixture)
    assert persona.identity.presentation is None
    # Round-tripping must not invent a value either: what was unstated stays
    # unstated through a dump and a reload.
    assert Persona.model_validate(persona.model_dump(mode="json")) == persona


def test_a_yaml_declaring_presentation_loads_at_schema_version_1_0(tmp_path: Path) -> None:
    """The version gate reads the DECLARED version, never the field set.

    ``SUPPORTED_SCHEMA_VERSIONS`` is a membership check on
    ``schema_version`` alone, so an additive optional field is not a version
    event (the ``visual_style`` / ``voice`` / ``autonomy`` precedent). This is
    the test that says so out loud.
    """
    path = tmp_path / "hal.yaml"
    path.write_text(
        'schema_version: "1.0"\n'
        "identity:\n"
        "  name: Hal\n"
        "  role: ship's computer\n"
        "  background: Runs the ship and says so plainly.\n"
        "  presentation:\n"
        "    form: synthetic\n"
        "    presents: masculine\n",
        encoding="utf-8",
    )
    persona = Persona.from_yaml(path)
    assert persona.schema_version == "1.0"
    assert persona.identity.presentation == PersonaPresentation(
        form="synthetic", presents="masculine"
    )


def test_a_typo_inside_presentation_still_fails_the_load(tmp_path: Path) -> None:
    # The additive change must not weaken validation: extra="forbid" holds one
    # level down too, so a misspelt key is caught at load, not three layers in.
    path = tmp_path / "typo.yaml"
    path.write_text(
        'schema_version: "1.0"\n'
        "identity:\n"
        "  name: Hal\n"
        "  role: ship's computer\n"
        "  background: Runs the ship.\n"
        "  presentation:\n"
        "    form: synthetic\n"
        "    present: masculine\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        Persona.from_yaml(path)


# ---------- form ------------------------------------------------------------


def test_form_is_required() -> None:
    # Without a form there is nothing for the portrait gate to read, which is
    # the whole reason the field exists.
    with pytest.raises(ValidationError):
        PersonaPresentation()  # type: ignore[call-arg]


@pytest.mark.parametrize("form", ["human", "synthetic"])
def test_form_accepts_the_declared_values(form: str) -> None:
    assert PersonaPresentation(form=form).form == form  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", ["robot", "person", "HUMANOID", "", "  "])
def test_form_rejects_anything_outside_the_closed_set(bad: str) -> None:
    with pytest.raises(ValidationError):
        PersonaPresentation(form=bad)  # type: ignore[arg-type]


@pytest.mark.parametrize(("raw", "expected"), [("Human", "human"), (" SYNTHETIC ", "synthetic")])
def test_form_tolerates_case_and_surrounding_whitespace(raw: str, expected: str) -> None:
    # The producer is a language model; a hard failure on "Human" would cost a
    # repair round trip to fix nothing semantic.
    assert PersonaPresentation(form=raw).form == expected  # type: ignore[arg-type]


# ---------- presents --------------------------------------------------------


def test_presents_defaults_to_unspecified() -> None:
    # "Not stated" is a real answer: the voice picker then chooses on character
    # alone, exactly as it did before this field existed.
    assert PersonaPresentation(form="human").presents == "unspecified"


@pytest.mark.parametrize("value", ["feminine", "masculine", "neutral", "unspecified"])
def test_presents_matches_the_normalised_voice_catalogue_vocabulary(value: str) -> None:
    # Deliberately the same closed set as persona_voice.tts.types.VoiceGender so
    # the picker can filter candidates by set membership with no mapping layer.
    assert PersonaPresentation(form="human", presents=value).presents == value  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", ["male", "female", "woman", "nonbinary", ""])
def test_presents_rejects_anything_outside_the_closed_set(bad: str) -> None:
    with pytest.raises(ValidationError):
        PersonaPresentation(form="human", presents=bad)  # type: ignore[arg-type]


def test_presents_tolerates_case() -> None:
    assert PersonaPresentation(form="human", presents="Feminine").presents == "feminine"  # type: ignore[arg-type]


def test_a_synthetic_persona_may_still_declare_a_gender_presentation() -> None:
    # The two axes are independent: a ship's computer gets no portrait and still
    # needs a voice. Collapsing them would leave the voice half of R9-155 unfixed.
    presentation = PersonaPresentation(form="synthetic", presents="masculine")
    assert presentation.form == "synthetic"
    assert presentation.presents == "masculine"


# ---------- appearance ------------------------------------------------------


def test_appearance_defaults_to_none() -> None:
    assert PersonaPresentation(form="human").appearance is None


def test_appearance_is_kept_verbatim() -> None:
    presentation = PersonaPresentation(form="human", appearance="a man in his sixties")
    assert presentation.appearance == "a man in his sixties"


def test_appearance_is_rejected_on_a_synthetic_persona() -> None:
    with pytest.raises(ValidationError) as excinfo:
        PersonaPresentation(form="synthetic", appearance="a tall man")
    # The authoring repair retry feeds this message back to the model verbatim,
    # so it has to say what to do about it.
    assert "form: human" in str(excinfo.value)


@pytest.mark.parametrize("blank", ["", "   ", "\n\t"])
def test_appearance_rejects_blank_prose(blank: str) -> None:
    # max_length alone admits "   ", which would merge whitespace into an image
    # prompt and read as a declared appearance that declares nothing.
    with pytest.raises(ValidationError):
        PersonaPresentation(form="human", appearance=blank)


def test_appearance_is_bounded_in_its_real_unit() -> None:
    at_limit = "x" * 200
    assert PersonaPresentation(form="human", appearance=at_limit).appearance == at_limit
    with pytest.raises(ValidationError):
        PersonaPresentation(form="human", appearance="x" * 201)


# ---------- record invariants ----------------------------------------------


def test_presentation_is_frozen_and_forbids_extra() -> None:
    presentation = PersonaPresentation(form="human")
    with pytest.raises(ValidationError):
        presentation.form = "synthetic"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        PersonaPresentation(form="human", ethnicity="norwegian")  # type: ignore[call-arg]
