"""Spec V12 T1 — the VOICE-mode emotion-adoption block (V12-D-2).

N5 CHAT-gated feeling-tag emission (voice stripped tags as a pure safety floor,
emitting none). V12 turns emission ON in voice so the persona declares a stance
tag the synthesis path maps to Cartesia expressivity — but the block MUST inherit
N5's hold-back-by-default, character-bounded restraint (most utterances no tag,
stoic personas ~none), instruct the model to LEAD with the tag (so it is captured
before the first spoken chunk), and make clear the tag is never spoken aloud.

The CHAT block (N5) stays byte-identical — proven by the frozen golden snapshot in
test_prompt_mode.py; here we prove the VOICE sibling exists, is voice-framed, and
does not leak the CHAT-specific emoji framing.
"""

from __future__ import annotations

from datetime import UTC, datetime

from persona.schema.chunks import PersonaChunk
from persona.schema.persona import Persona, PersonaIdentity
from persona_runtime.prompt import (
    EMOTION_ADOPTION_VOICE_VERSION,
    PromptBuilder,
    PromptMode,
    RetrievedContext,
)


def _chunk(text: str, meta: dict[str, str] | None = None) -> PersonaChunk:
    return PersonaChunk(
        id=f"id-{abs(hash(text)) % 10000}",
        text=text,
        metadata=meta or {},
        distance=None,
        created_at=datetime(2020, 1, 1, tzinfo=UTC),
    )


def _inputs() -> tuple[Persona, RetrievedContext, dict[str, object]]:
    persona = Persona(
        persona_id="astrid",
        identity=PersonaIdentity(
            name="Astrid",
            role="Norwegian tenancy law assistant",
            background="Knows husleieloven.",
            constraints=["Never give binding advice."],
        ),
        tools=[],
    )
    ctx = RetrievedContext(
        self_facts=[_chunk("I specialise in tenancy law.")],
        worldview=[_chunk("Tenants have strong protections.", {"epistemic": "fact"})],
        episodic=[_chunk("Last time we discussed mould.")],
    )
    kwargs: dict[str, object] = {
        "skill_index": "Available skills:\n- web_research",
        "user_message": "What are my rights?",
        "max_tokens": 8000,
        "matched_skill_content": "SKILL: do web research carefully.",
    }
    return persona, ctx, kwargs


def _voice_system() -> str:
    persona, ctx, kwargs = _inputs()
    return PromptBuilder().build(persona, ctx, [], mode=PromptMode.VOICE, **kwargs)[0].content  # type: ignore[arg-type]


def _chat_system() -> str:
    persona, ctx, kwargs = _inputs()
    return PromptBuilder().build(persona, ctx, [], mode=PromptMode.CHAT, **kwargs)[0].content  # type: ignore[arg-type]


# A stable marker unique to the VOICE emotion block (voice-framed, not emoji).
_VOICE_EMOTION_MARKER = "colour how you sound"


class TestVoiceEmotionAdoption:
    def test_present_in_voice(self) -> None:
        assert _VOICE_EMOTION_MARKER in _voice_system()

    def test_voice_emits_tags_now(self) -> None:
        """V12: voice declares stance tags (the {{#tag}} form) — no longer stripped-only."""
        assert "{{#happy}}" in _voice_system()

    def test_tag_is_never_spoken(self) -> None:
        """The tag guides the voice; it is never read aloud (out-of-band signal)."""
        assert "never spoken" in _voice_system()

    def test_leads_with_the_tag(self) -> None:
        """Lead-with-tag: captured before the first spoken chunk (V12-D-2 timing)."""
        system = _voice_system()
        assert "at the very start" in system or "Lead with the tag" in system

    def test_inherits_hold_back_restraint(self) -> None:
        """N5's restraint carried into voice: hold-back-by-default, exception-not-rule."""
        system = _voice_system()
        assert "Hold back by default" in system
        assert "the exception, not the rule" in system
        assert "reserved persona uses almost none" in system

    def test_bounded_by_character_below_lock_above_memory(self) -> None:
        system = _voice_system()
        lock_pos = system.index("Inhabit your character")
        emotion_pos = system.index(_VOICE_EMOTION_MARKER)
        assert lock_pos < emotion_pos
        assert emotion_pos < system.index("Relevant facts about yourself:")

    def test_uses_only_the_versioned_palette(self) -> None:
        system = _voice_system()
        assert "proud_of_you" in system  # warm-biased, other-directed
        for banned in ("disappointed", "angry", "disgust"):
            assert banned not in system, banned

    def test_chat_emoji_framing_never_leaks_to_voice(self) -> None:
        """The CHAT block's emoji-specific instruction is chat-only (voice has no emojis)."""
        assert "never type an emoji yourself" not in _voice_system()

    def test_voice_block_absent_from_chat(self) -> None:
        """The voice-framed emotion block never appears in a chat prompt."""
        assert _VOICE_EMOTION_MARKER not in _chat_system()

    def test_version_constant_is_set(self) -> None:
        assert EMOTION_ADOPTION_VOICE_VERSION  # versioned artifact (Spec 10 discipline)
