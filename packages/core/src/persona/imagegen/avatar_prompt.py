"""Deterministic avatar-prompt crafter (Spec 29 T1, D-29-1, revised by R9-155).

Turns a persona's *declared* identity into a role-anchored, PII-free image
prompt for build-time avatar auto-generation. The crafted prompt flows
through the same Spec 15 defences as any ``generate_image`` call (the
:mod:`persona.imagegen.safety` hard-line filter + provider moderation);
this module's contract is that a well-formed persona yields a prompt that
passes the hard line cleanly.

**R9-155 supersedes part of D-29-1, and it is worth being precise about
which part.** D-29-1 was right that inferring gender or ethnicity from a
persona's NAME is stereotyping, and that rule still holds absolutely: the
name is not read here, and two personas differing only by name produce
byte-identical prompts. What D-29-1 got wrong was the remedy. Leaving the
prompt demographic-silent did not avoid a demographic choice; it handed the
choice to the image model's priors for the role, where nobody could see it
and nothing could correct it. That is how personas described as men came
back as portraits of women. The fix is not to guess better, it is to let
the persona SAY, once, in a field a person can read and edit.

**The field map.**

* ``presentation.presents`` → the **subject**. ``feminine`` / ``masculine``
  / ``neutral`` put a woman / a man / a person in the portrait; and
  ``unspecified`` (or no ``presentation`` at all) puts nothing there, which
  is the pre-R9-155 prompt byte for byte. Authored, never inferred.
* ``presentation.appearance`` → declared detail about **who is in the
  portrait**, appended verbatim. Absent for most personas, and silence here
  means the prompt stays silent.
* ``role`` → the **professional anchor**. ``role`` is
  ``Field(min_length=1)`` so it is always present.
* ``visual_style`` → **how the portrait is rendered**, the medium rather
  than the subject. It historically carried both, because it was the only
  channel there was; ``appearance`` now owns the subject half. Routed
  through the existing :func:`merge_visual_style`.
* ``background`` → **NOT parsed.** Free-prose extraction is exactly the
  demographic-leakage vector D-29-1 forbids ("background states an age"),
  and ``presentation`` now provides the declared route that makes parsing
  prose unnecessary as well as forbidden.
* ``name`` → **OMITTED**, and this has not changed. You cannot
  name-stereotype a name that is not there (the "David → male, 35"
  failure); omission also keeps the prompt PII-free (an *archetype*, not a
  named individual).
* ``constraints`` → not used (behavioural, not visual).

The crafter **never** injects diversity / counter-stereotype specifiers
("diverse", "of any ethnicity") — the bias literature shows they are
model-specific and overcorrect (research §2.2.2). Where nothing is
declared it stays **silent** on demographics and conservative + role-
anchored, letting the provider's own distribution stand rather than
steering it with a guess.

**Determinism.** The single public function :func:`craft_avatar_prompt`
is pure: no seed, no randomness, no timestamp, no I/O, no model call. The
same :class:`~persona.schema.persona.PersonaIdentity` always yields a
byte-identical prompt, so a rebuild reproduces the prompt and the content-
addressed ``blake2b`` persist (Spec 15) collapses identical bytes — the
avatar is a stable function of the persona.

References:
    docs/specs/phase2/spec_29/decisions.md D-29-1;
    docs/specs/phase2/spec_29/research.md §2.2.1–§2.2.3;
    docs/specs/phase2/spec_29/spec_29_avatar_generation.md §2.1.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.imagegen._merge import merge_visual_style
from persona.imagegen.errors import SyntheticPersonaHasNoPortraitError

if TYPE_CHECKING:
    from persona.schema.persona import PersonaIdentity

__all__ = ["craft_avatar_prompt", "wants_generated_portrait"]


#: Fixed portrait scaffolding appended after the role anchor. These are
#: demographic-silent professional-headshot quality descriptors drawn from
#: the headshot-prompt prior art (research §2.2.2) — attire framing, a
#: neutral background, and soft lighting. They contain no minor / sexual /
#: demographic vocabulary, so they cannot contribute to a hard-line
#: co-occurrence trigger (D-29-6).
_PORTRAIT_SCAFFOLD: str = "professional attire, neutral studio background, soft even lighting"

#: The opening used when no subject is declared. Kept as a constant so the
#: no-presentation branch is visibly the SAME string it has always been.
_PORTRAIT_OPENING: str = "a professional headshot portrait"

#: Fallback subject when ``role`` normalises to empty (a whitespace-only
#: authored role still satisfies ``min_length`` but carries no content).
#: Conservative + role-anchored + demographic-silent.
_FALLBACK_ROLE: str = "a professional"

#: The declared ``presents`` value → the subject of the portrait (R9-155).
#: Plain, unadorned nouns: the point is to say what the author said, not to
#: steer the model with adjectives it will over-apply. ``"unspecified"`` is
#: absent from this map on purpose, and so is a missing ``presentation``:
#: both mean the author did not say, and the prompt then says nothing, which
#: is exactly the prompt this crafter produced before the field existed.
#:
#: ``neutral`` maps to "a person", which is deliberately the weakest entry
#: here. A gender-neutral VOICE is a concrete thing a catalogue can offer; a
#: gender-neutral FACE is a far vaguer instruction, and the alternatives
#: ("androgynous") invite caricature. "a person" says only what was declared.
_SUBJECT_BY_PRESENTS: dict[str, str] = {
    "feminine": "a woman",
    "masculine": "a man",
    "neutral": "a person",
}


def _normalise_role(role: str) -> str:
    """Collapse whitespace in the declared role; fall back if empty.

    Whitespace normalisation gives stable, byte-identical output for
    trivially-different inputs; the fallback guards the degenerate
    whitespace-only authored role. No casing change is applied — image
    models are case-insensitive and lowering would mangle proper nouns
    (``"Norwegian"``).

    Args:
        role: The persona's ``identity.role`` (``min_length=1``).

    Returns:
        The role with internal whitespace runs collapsed to single
        spaces and surrounding whitespace stripped, or
        :data:`_FALLBACK_ROLE` if nothing remains.
    """
    collapsed = " ".join(role.split())
    return collapsed or _FALLBACK_ROLE


def wants_generated_portrait(identity: PersonaIdentity) -> bool:
    """Should anything draw a portrait for this persona at all? (R9-155)

    ``False`` for a persona whose authored ``presentation.form`` is
    ``"synthetic"``: it is drawn as its own generated mark instead, which costs
    no generation call, cannot fail, and reads as deliberate rather than as a
    portrait that went wrong. ``True`` for everyone else, INCLUDING every
    persona that predates the field. An unstated presentation means unstated,
    not synthetic, so nothing that exists today changes behaviour.

    This is the one question the avatar gates ask, so they cannot answer it
    differently from each other. Pure: no I/O, no model call.

    Args:
        identity: The persona's :class:`~persona.schema.persona.PersonaIdentity`.

    Returns:
        Whether a generated portrait applies to this persona.
    """
    presentation = identity.presentation
    return presentation is None or presentation.form != "synthetic"


def craft_avatar_prompt(identity: PersonaIdentity) -> str:
    """Craft a deterministic avatar prompt from what the persona declares.

    Builds a role-anchored professional portrait from the persona's
    *declared* identity: the subject from ``presentation`` (R9-155), the
    professional anchor from ``role``, and the rendering style merged from
    ``visual_style`` via :func:`merge_visual_style`. The ``name`` is
    omitted and ``background`` is not parsed (see the module docstring for
    the full field map and the D-29-1 history).

    A persona with no ``presentation``, or one whose ``presents`` is
    ``"unspecified"``, yields the prompt this function produced before the
    field existed, byte for byte. That is the property that lets every
    persona already on disk keep working, and it is pinned by a golden test
    rather than left to inspection.

    Args:
        identity: The persona's :class:`~persona.schema.persona.PersonaIdentity`.
            Only ``presentation``, ``role`` and ``visual_style`` influence
            the output; ``name`` / ``background`` / ``constraints`` are
            deliberately ignored.

    Returns:
        The image prompt to pass to the Spec 15 imagegen pipeline. Pure
        function of ``identity`` — byte-identical across calls with equal
        input, and carrying a demographic token only where the persona
        declared one.

    Raises:
        SyntheticPersonaHasNoPortraitError: The persona is authored
            ``presentation.form: synthetic``, so it has no portrait to craft
            a prompt for. Callers ask :func:`wants_generated_portrait` first;
            this is the backstop that makes a missed gate decline rather than
            quietly draw a human face.
    """
    if not wants_generated_portrait(identity):
        raise SyntheticPersonaHasNoPortraitError(
            "this persona is drawn as its own mark, not as a portrait",
            context={"form": "synthetic"},
        )
    role = _normalise_role(identity.role)
    presentation = identity.presentation
    subject = (
        _SUBJECT_BY_PRESENTS.get(presentation.presents, "") if presentation is not None else ""
    )
    # "of a man representing the role of surgeon" reads as one clause, so the
    # role still qualifies the subject rather than competing with it.
    opening = f"a professional headshot portrait of {subject}" if subject else _PORTRAIT_OPENING
    appearance = presentation.appearance if presentation is not None else None
    declared = f", {appearance}" if appearance else ""
    base = f"{opening} representing the role of {role}{declared}, {_PORTRAIT_SCAFFOLD}"
    return merge_visual_style(base, identity.visual_style)
