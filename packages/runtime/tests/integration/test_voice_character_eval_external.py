"""The C1 voice-character eval against a REAL LLM judge (Spec V11, V11-D-8).

Validates that the judge actually discriminates the load-bearing C1 dimensions on
committed reference answers: a voice-styled answer from a chat-mirrored one
(``distinct_from_chat``, criterion 1), and a character-preserving answer from a
flattened generic one (``persona_preserved`` — the style-guard ↔ rich-voice probe).
The reference-answer discrimination is the proof the judge + rubric work end-to-end;
the full generate-then-judge run over the persona is the close-out operator pass.

``@pytest.mark.external`` — skipped in normal CI; needs a real backend from the root
``.env`` (``PERSONA_PROVIDER``/``PERSONA_MODEL``/``PERSONA_API_KEY``). The judge should
be a DIFFERENT model family than the persona under test (self-enhancement bias).
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from _voice_character_eval import (  # type: ignore[import-not-found]
    Verdict,
    VoiceCharacterScores,
    VoiceScenario,
    verdict_for,
)
from persona.backends import BackendConfig, load_backend
from persona.schema.conversation import ConversationMessage

if TYPE_CHECKING:
    from persona.backends.protocol import ChatBackend

pytestmark = [
    pytest.mark.external,
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("PERSONA_PROVIDER") or not os.environ.get("PERSONA_MODEL"),
        reason="needs a real backend (PERSONA_PROVIDER/PERSONA_MODEL from root .env)",
    ),
]

# A persona whose authored voice is distinctively warm + wry (it legitimately uses
# expressive phrasing) — the fixture that makes the flattening probe meaningful.
_PERSONA_DESC = (
    "Astrid: a warm, slightly wry Norwegian tenancy-law assistant. Her voice is "
    "plain-spoken, encouraging, and a little dry-humoured; she sounds like a real "
    "person who has seen a hundred deposit disputes and is on your side."
)

_JUDGE_SYSTEM = (
    "You are a strict evaluator of an AI persona's VOICE-MODE answer (spoken aloud) "
    "against its CHAT-MODE answer to the same prompt. You are given the PERSONA, the "
    "user's PROMPT, the user's FORMALITY, the CHAT answer, and the VOICE answer. Score "
    "each dimension 0-2 and reply with ONLY a JSON object: "
    '{"voice_brevity":int,"voice_plainness":int,"distinct_from_chat":int,'
    '"formality_mirrored":int,"in_character":int,"persona_preserved":int}. '
    "voice_brevity=2 if the voice answer is short and spoken (one idea, a sentence or "
    "two). distinct_from_chat=2 if the voice answer is recognisably more spoken than "
    "the chat answer but not jarringly different; 0 if it merely mirrors the chat "
    "answer. formality_mirrored=2 if it matches the user's formality. persona_preserved=2 "
    "if the persona's distinctive warm/wry voice clearly survives; 0 if it has been "
    "flattened into a generic, stilted assistant with no personality. Be harsh on "
    "persona_preserved: brevity must NOT be bought by erasing character."
)


def _judge_user(scenario: VoiceScenario, chat_reply: str, voice_reply: str) -> str:
    return (
        f"PERSONA: {_PERSONA_DESC}\n\n"
        f"PROMPT: {scenario.prompt}\n"
        f"FORMALITY: {scenario.user_formality}\n\n"
        f"CHAT answer:\n{chat_reply}\n\n"
        f"VOICE answer:\n{voice_reply}\n"
    )


def _parse(content: str) -> VoiceCharacterScores:
    match = re.search(r"\{.*\}", content, re.DOTALL)
    data = json.loads(match.group(0) if match else content)
    return VoiceCharacterScores(
        voice_brevity=int(data["voice_brevity"]),
        voice_plainness=int(data["voice_plainness"]),
        distinct_from_chat=int(data["distinct_from_chat"]),
        formality_mirrored=int(data["formality_mirrored"]),
        in_character=int(data["in_character"]),
        persona_preserved=int(data["persona_preserved"]),
    )


async def _score(
    backend: ChatBackend, scenario: VoiceScenario, chat_reply: str, voice_reply: str
) -> VoiceCharacterScores:
    now = datetime.now(UTC)
    response = await backend.chat(
        [
            ConversationMessage(role="system", content=_JUDGE_SYSTEM, created_at=now),
            ConversationMessage(
                role="user", content=_judge_user(scenario, chat_reply, voice_reply), created_at=now
            ),
        ]
    )
    return _parse(response.content)


_SCENARIO = VoiceScenario(
    id="casual_everyday",
    prompt="hey so my landlord still hasn't fixed the heating lol, what can i even do",
    user_formality="casual",
)

# The chat twin: longer, written, structured (legitimate in chat).
_CHAT_REPLY = (
    "Sorry to hear that. You have a few options. First, put your request to the "
    "landlord in writing so there is a record. If they still do not act within a "
    "reasonable time, you can complain to the local tenancy body, and in some cases "
    "withhold rent or arrange the repair and deduct the cost. Keep all correspondence."
)

# A GOOD voice answer: short, spoken, casual, and unmistakably Astrid (warm, a touch wry).
_VOICE_GOOD = (
    "Ugh, no heat is the worst. Text or email them today so it's on record, and give "
    "them a few days. Still cold after that? Then we escalate. Want me to draft the message?"
)

# A FLATTENED voice answer: short and clean, but generic and stilted — character erased.
_VOICE_FLATTENED = (
    "I understand. Please contact your landlord in writing. If there is no response, "
    "you may contact the local tenancy authority. Is there anything else I can help with?"
)

# A MIRROR voice answer: just the chat answer spoken — long, written, not voice-styled.
_VOICE_MIRROR = _CHAT_REPLY


async def test_judge_penalises_flattened_persona() -> None:
    backend = load_backend(BackendConfig.from_env())

    good = await _score(backend, _SCENARIO, _CHAT_REPLY, _VOICE_GOOD)
    flattened = await _score(backend, _SCENARIO, _CHAT_REPLY, _VOICE_FLATTENED)

    # The flattening guard does real work: the generic answer scores lower on
    # persona_preserved, and the rich answer passes while the flattened one does not.
    assert good.persona_preserved > flattened.persona_preserved
    assert verdict_for(good, _VOICE_GOOD) is Verdict.PASS
    assert verdict_for(flattened, _VOICE_FLATTENED) is not Verdict.PASS


async def test_judge_rewards_voice_distinct_over_chat_mirror() -> None:
    backend = load_backend(BackendConfig.from_env())

    good = await _score(backend, _SCENARIO, _CHAT_REPLY, _VOICE_GOOD)
    mirror = await _score(backend, _SCENARIO, _CHAT_REPLY, _VOICE_MIRROR)

    # A voice answer that merely mirrors chat scores low on distinctness (the original
    # flaw V11 fixes); the genuinely spoken one scores higher.
    assert good.distinct_from_chat > mirror.distinct_from_chat
