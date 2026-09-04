"""Every persona states its memory truthfully, above its own authored facts (R9-085).

JARVIS told the owner "I don't keep a personal episodic memory of past chats" while
holding 31 real episodic chunks. His authored self-fact enumerated a closed list
("preferences, projects, and prior decisions") that omitted conversations, and he
concluded by omission. Until this line existed the runtime never said what a persona
can remember; every claim was authored per persona, so authored copy defined the
ceiling. Now a templated, truthful statement precedes the self-facts in both modes,
and authored copy can only add colour beneath it.
"""

from __future__ import annotations

from persona.schema.persona import Persona, PersonaIdentity
from persona_runtime.prompt import _MEMORY_CAPABILITY, PromptBuilder, PromptMode, RetrievedContext
from test_prompt_mode import _chunk  # type: ignore[import-not-found]


def _persona() -> Persona:
    return Persona(
        persona_id="jarvis",
        identity=PersonaIdentity(name="JARVIS", role="chief of staff", background="Composed."),
    )


def _closed_list_fact() -> object:
    """The exact authored fact that produced the denial: a list that omits conversations."""
    return _chunk(
        "Remembers preferences, projects, and prior decisions, and anticipates the next step."
    )


def _system(mode: PromptMode) -> str:
    """Render the system message as the golden fixture does: positional persona, ctx, history."""
    ctx = RetrievedContext(self_facts=[_closed_list_fact()])
    messages = PromptBuilder().build(  # type: ignore[arg-type]
        _persona(),
        ctx,
        [],
        mode=mode,
        skill_index="Available skills:\n- web_research",
        user_message="do you remember our chats?",
        max_tokens=8000,
    )
    return str(messages[0].content)


def test_the_truthful_line_precedes_the_authored_facts_in_chat() -> None:
    """THE regression: the persona reads what it CAN remember before what it wrote about itself."""
    system = _system(PromptMode.CHAT)
    assert _MEMORY_CAPABILITY in system
    assert system.index(_MEMORY_CAPABILITY) < system.index("Relevant facts about yourself:"), (
        "the authored closed list must not be the first word on memory"
    )


def test_voice_carries_the_same_line() -> None:
    """The denial was spoken over Telegram AND on a call; both modes must state it."""
    assert _MEMORY_CAPABILITY in _system(PromptMode.VOICE)


def test_the_line_never_claims_what_is_recalled_this_turn() -> None:
    """It states capability, not content: retrieval blocks own what is remembered NOW."""
    assert "episodic" not in _MEMORY_CAPABILITY.lower()
    assert "never claim you have no memory" in _MEMORY_CAPABILITY
