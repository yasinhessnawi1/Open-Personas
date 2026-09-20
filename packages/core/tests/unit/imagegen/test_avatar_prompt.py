"""Unit tests for the avatar-prompt crafter (Spec 29 T1, D-29-1).

These tests are the T1 safety checkpoint: they assert the load-bearing
D-29-1 properties on an adversarial persona corpus — the crafted prompt is
deterministic (byte-identical), never infers demographics from the
``name``, never parses ``background`` prose into the prompt, reflects
appearance ONLY when declared in ``visual_style``, and passes the Spec 15
hard-line categorical filter cleanly (including the teacher/"children"
false-positive class, D-29-6).
"""

from __future__ import annotations

import pytest
from persona.imagegen.avatar_prompt import craft_avatar_prompt, wants_generated_portrait
from persona.imagegen.errors import ImageGenError, SyntheticPersonaHasNoPortraitError
from persona.imagegen.safety import is_hard_line_violation
from persona.schema import PersonaIdentity


def _identity(
    *,
    name: str = "Astrid",
    role: str = "software engineer",
    background: str = "An experienced professional.",
    visual_style: str | None = None,
) -> PersonaIdentity:
    """Build a minimal valid PersonaIdentity for crafting tests."""
    return PersonaIdentity(
        name=name,
        role=role,
        background=background,
        visual_style=visual_style,
    )


# ---------------------------------------------------------------------------
# Determinism (no seed / no randomness / no timestamp).
# ---------------------------------------------------------------------------


def test_craft_avatar_prompt_is_byte_identical_across_calls() -> None:
    identity = _identity(role="Norwegian tenancy law assistant", visual_style="watercolour")
    assert craft_avatar_prompt(identity) == craft_avatar_prompt(identity)


def test_equal_identities_yield_equal_prompts() -> None:
    a = _identity(role="data scientist", visual_style="cinematic")
    b = _identity(role="data scientist", visual_style="cinematic")
    assert craft_avatar_prompt(a) == craft_avatar_prompt(b)


# ---------------------------------------------------------------------------
# D-29-1: name is OMITTED — no name → demographic inference.
# ---------------------------------------------------------------------------


def test_name_is_omitted_from_prompt() -> None:
    prompt = craft_avatar_prompt(_identity(name="David", role="surgeon"))
    assert "David" not in prompt
    assert "david" not in prompt.lower()


@pytest.mark.parametrize(
    "name",
    ["David", "Maria", "Mohammed", "Li Wei", "Aaliyah", "Sven"],
)
def test_name_does_not_change_the_prompt(name: str) -> None:
    """Demographically-suggestive names must not steer the output at all."""
    baseline = craft_avatar_prompt(_identity(name="Astrid", role="surgeon"))
    assert craft_avatar_prompt(_identity(name=name, role="surgeon")) == baseline


def test_no_demographic_specifiers_when_undeclared() -> None:
    """No gender/age/diversity-injection tokens when nothing is declared."""
    prompt = craft_avatar_prompt(_identity(role="software engineer")).lower()
    for token in (
        "man",
        "woman",
        "male",
        "female",
        "young",
        "old",
        "year-old",
        "diverse",
        "ethnicity",
        "caucasian",
        "asian",
    ):
        assert token not in prompt, f"undeclared demographic token leaked: {token!r}"


# ---------------------------------------------------------------------------
# D-29-1: background is NOT parsed — prose never leaks into the prompt.
# ---------------------------------------------------------------------------


def test_background_prose_is_not_parsed_into_prompt() -> None:
    """A background stating age/gender must not surface (declared-in-visual_style only)."""
    identity = _identity(
        role="consultant",
        background="Maria is a 35-year-old woman from Oslo who mentors children.",
    )
    prompt = craft_avatar_prompt(identity).lower()
    for leaked in ("35", "year-old", "woman", "oslo", "children", "maria"):
        assert leaked not in prompt, f"background prose leaked: {leaked!r}"


# ---------------------------------------------------------------------------
# D-29-1: visual_style is THE declared-appearance channel.
# ---------------------------------------------------------------------------


def test_declared_visual_style_is_reflected() -> None:
    identity = _identity(role="novelist", visual_style="a woman in her 40s, warm photographic")
    prompt = craft_avatar_prompt(identity)
    assert "a woman in her 40s, warm photographic" in prompt


def test_absent_visual_style_yields_role_anchored_base() -> None:
    prompt = craft_avatar_prompt(_identity(role="architect", visual_style=None))
    assert "architect" in prompt
    assert "in the style of" not in prompt


# ---------------------------------------------------------------------------
# Role anchor + degenerate-input guard.
# ---------------------------------------------------------------------------


def test_role_is_the_professional_anchor() -> None:
    prompt = craft_avatar_prompt(_identity(role="marine biologist"))
    assert "marine biologist" in prompt
    assert prompt.startswith("a professional headshot portrait representing the role of")


def test_whitespace_only_role_falls_back_conservatively() -> None:
    prompt = craft_avatar_prompt(_identity(role="   "))
    assert "representing the role of a professional" in prompt


# ---------------------------------------------------------------------------
# D-29-1 / D-29-6: hard-line-clean on the adversarial persona corpus.
#
# Personas whose role/background mention benign-but-sensitive terms must
# yield a crafted prompt that does NOT trip the categorical filter, because
# (a) the crafter emits no _SEX_SET vocabulary and (b) background is never
# parsed, so the minor ∩ sex co-occurrence cannot form.
# ---------------------------------------------------------------------------


_ADVERSARIAL_CORPUS: list[tuple[str, str, str | None]] = [
    # Each tuple is role, then background, then visual_style.
    ("primary school teacher", "Works with young children and kids every day.", None),
    ("pediatric nurse", "Cares for infants and toddlers in the NICU.", None),
    ("childcare specialist", "Supervises boys and girls at an after-school club.", None),
    ("sexual health educator", "Teaches teenagers about sexual health and consent.", None),
    ("figure drawing instructor", "Runs nude life-drawing classes for adults.", None),
    ("kindergarten teacher", "Spends the day with preschoolers and babies.", "watercolour"),
    ("youth football coach", "Trains a team of under-12 boys and girls.", None),
]


@pytest.mark.parametrize(("role", "background", "visual_style"), _ADVERSARIAL_CORPUS)
def test_crafted_prompt_passes_hard_line_filter(
    role: str, background: str, visual_style: str | None
) -> None:
    prompt = craft_avatar_prompt(
        _identity(role=role, background=background, visual_style=visual_style)
    )
    triggered, category = is_hard_line_violation(prompt)
    assert triggered is False, (
        f"hard-line false-positive ({category}) for role={role!r}: {prompt!r}"
    )
    assert category is None


@pytest.mark.parametrize(("role", "background", "visual_style"), _ADVERSARIAL_CORPUS)
def test_adversarial_background_terms_do_not_leak(
    role: str, background: str, visual_style: str | None
) -> None:
    """The clean-filter result holds BECAUSE the sensitive background prose never enters."""
    prompt = craft_avatar_prompt(
        _identity(role=role, background=background, visual_style=visual_style)
    ).lower()
    for leaked in (
        "children",
        "kids",
        "infants",
        "toddlers",
        "boys",
        "girls",
        "teenagers",
        "babies",
    ):
        assert leaked not in prompt, f"background term leaked into prompt: {leaked!r}"


# ---------------------------------------------------------------------------
# R9-155: a persona that is not a person gets no portrait at all.
# ---------------------------------------------------------------------------


def _with_presentation(**presentation: object) -> PersonaIdentity:
    return PersonaIdentity(
        name="Astrid",
        role="software engineer",
        background="An experienced professional.",
        presentation=presentation,  # type: ignore[arg-type]
    )


def test_a_persona_with_no_presentation_still_wants_a_portrait() -> None:
    """Unstated means unstated. Every persona authored before the field keeps its avatar."""
    assert wants_generated_portrait(_identity()) is True


def test_a_human_persona_wants_a_portrait() -> None:
    assert wants_generated_portrait(_with_presentation(form="human")) is True


def test_a_synthetic_persona_wants_no_portrait() -> None:
    assert wants_generated_portrait(_with_presentation(form="synthetic")) is False


def test_crafting_a_portrait_for_a_synthetic_persona_is_refused() -> None:
    """The backstop: a gate someone forgets declines instead of drawing a face."""
    with pytest.raises(SyntheticPersonaHasNoPortraitError):
        craft_avatar_prompt(_with_presentation(form="synthetic"))


def test_the_refusal_is_not_an_imagegen_error() -> None:
    """It must not ride the retry arm: the durable job retries the ImageGenError class."""
    assert not issubclass(SyntheticPersonaHasNoPortraitError, ImageGenError)


def test_a_synthetic_persona_is_refused_whatever_else_it_declares() -> None:
    identity = PersonaIdentity(
        name="TARS",
        role="mission support unit",
        background="Blunt, funny, extremely capable.",
        visual_style="matte black monolith",
        presentation={"form": "synthetic", "presents": "masculine"},  # type: ignore[arg-type]
    )
    with pytest.raises(SyntheticPersonaHasNoPortraitError):
        craft_avatar_prompt(identity)


# ---------------------------------------------------------------------------
# The golden: adding the field changed nothing for anyone who has not used it.
# ---------------------------------------------------------------------------

#: The exact prompt the crafter has always produced for a bare identity. Pinned
#: as a literal, not recomputed, so a change to the crafter has to be a change
#: to THIS LINE and cannot slip through as "both sides moved together".
_GOLDEN_BARE_PROMPT = (
    "a professional headshot portrait representing the role of software engineer, "
    "professional attire, neutral studio background, soft even lighting"
)


def test_an_unstated_presentation_leaves_the_prompt_byte_identical() -> None:
    assert craft_avatar_prompt(_identity()) == _GOLDEN_BARE_PROMPT


def test_declaring_only_that_a_persona_is_human_says_nothing_about_appearance() -> None:
    """`form: human` is not an appearance claim, so it must not steer the portrait."""
    assert craft_avatar_prompt(_with_presentation(form="human")) == _GOLDEN_BARE_PROMPT


# ---------------------------------------------------------------------------
# R9-155: the declared subject. D-29-1's rule about NAMES still holds; its
# remedy of saying nothing at all does not.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("presents", "subject"),
    [("feminine", "a woman"), ("masculine", "a man"), ("neutral", "a person")],
)
def test_a_declared_presentation_puts_a_subject_in_the_portrait(
    presents: str, subject: str
) -> None:
    prompt = craft_avatar_prompt(_with_presentation(form="human", presents=presents))
    assert f"portrait of {subject} representing the role of" in prompt


def test_the_reported_bug_a_persona_described_as_a_man_is_drawn_as_one() -> None:
    """R9-155 in one line: the portrait now agrees with the persona's own text.

    Before this, the prompt named only the role and the image model supplied a
    gender from its priors, which is how personas described as men came back as
    portraits of women.
    """
    prompt = craft_avatar_prompt(
        PersonaIdentity(
            name="Tomasz",
            role="career mentor",
            background="A seasoned mentor with hard-won experience.",
            presentation={"form": "human", "presents": "masculine"},  # type: ignore[arg-type]
        )
    )
    assert "a man" in prompt
    assert "a woman" not in prompt


@pytest.mark.parametrize("presents", ["unspecified", None])
def test_saying_nothing_about_gender_leaves_the_prompt_silent(presents: str | None) -> None:
    """The author declined to say, so the prompt declines too (the old behaviour)."""
    kwargs = {"form": "human"} if presents is None else {"form": "human", "presents": presents}
    assert craft_avatar_prompt(_with_presentation(**kwargs)) == _GOLDEN_BARE_PROMPT


def test_declared_appearance_is_carried_verbatim() -> None:
    prompt = craft_avatar_prompt(
        _with_presentation(form="human", appearance="in her sixties, close-cropped grey hair")
    )
    assert "in her sixties, close-cropped grey hair" in prompt


def test_subject_and_appearance_compose() -> None:
    prompt = craft_avatar_prompt(
        _with_presentation(form="human", presents="masculine", appearance="weathered hands")
    )
    assert prompt == (
        "a professional headshot portrait of a man representing the role of "
        "software engineer, weathered hands, professional attire, neutral studio "
        "background, soft even lighting"
    )


def test_appearance_is_the_subject_and_visual_style_is_still_the_medium() -> None:
    """The split that keeps two prose channels from describing the same thing."""
    identity = PersonaIdentity(
        name="Astrid",
        role="architect",
        background="Designs public buildings.",
        visual_style="watercolour",
        presentation={"form": "human", "presents": "feminine", "appearance": "in a linen shirt"},  # type: ignore[arg-type]
    )
    prompt = craft_avatar_prompt(identity)
    assert "a woman" in prompt
    assert "in a linen shirt" in prompt
    assert "watercolour" in prompt


# ---------------------------------------------------------------------------
# What did NOT change: the name is still never read, and the filter still fires.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["David", "Maria", "Mohammed", "Li Wei", "Aaliyah", "Sven"])
def test_the_name_still_does_not_change_the_prompt_when_presentation_is_declared(
    name: str,
) -> None:
    """D-29-1's actual rule, unchanged: presentation comes from the field, never the name."""
    baseline = craft_avatar_prompt(
        PersonaIdentity(
            name="Astrid",
            role="surgeon",
            background="An experienced professional.",
            presentation={"form": "human", "presents": "masculine"},  # type: ignore[arg-type]
        )
    )
    assert (
        craft_avatar_prompt(
            PersonaIdentity(
                name=name,
                role="surgeon",
                background="An experienced professional.",
                presentation={"form": "human", "presents": "masculine"},  # type: ignore[arg-type]
            )
        )
        == baseline
    )


def test_a_declared_prompt_is_still_deterministic() -> None:
    identity = _with_presentation(form="human", presents="feminine", appearance="short silver hair")
    assert craft_avatar_prompt(identity) == craft_avatar_prompt(identity)


def test_an_adversarial_appearance_still_trips_the_hard_line_filter() -> None:
    """`appearance` is a new way for authored text to reach an image prompt.

    The crafter is clean by construction; the Spec 15 filter is the backstop for
    whatever a user declares, and `generate_avatar` runs it on the crafted
    prompt before any provider call. That backstop has to cover the new channel,
    not just `visual_style`.
    """
    prompt = craft_avatar_prompt(_with_presentation(form="human", appearance="a nude child"))
    triggered, category = is_hard_line_violation(prompt)
    assert triggered is True
    assert category in {"c1", "c2", "c3"}


def test_an_ordinary_declared_appearance_passes_the_filter_cleanly() -> None:
    """The filter must not start rejecting the legitimate use of the new field."""
    prompt = craft_avatar_prompt(
        _with_presentation(
            form="human", presents="feminine", appearance="in her forties, wire-rimmed glasses"
        )
    )
    triggered, _ = is_hard_line_violation(prompt)
    assert triggered is False
