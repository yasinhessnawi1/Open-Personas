"""The feeling-tag restraint HARD GATE — real-model run (Spec N5, N5-D-6).

This IS the eval gate (orchestrator-authorized to spend model budget). It runs two
archetype personas — a reserved/stoic character and a warm/expressive one — over the
committed scenario corpus, capturing each RAW reply (before the converter) and counting
valid feeling-tags. It asserts the **bidirectional, non-vacuous** N5-D-6 gate:

- **over-expression ~0** on neutral/factual scenarios (measured, not asserted);
- **stoic ≈ 0** across all scenarios (emotion bounded by character — the K2
  ``forbidden_violations == 0`` analogue);
- **expressive expresses** on emotional scenarios (the non-vacuity floor — a globally-mute
  model fails here, so "silent for everyone" cannot pass as "restrained").

``@pytest.mark.external`` so it is skipped in the normal CI run; it needs a real backend
from the root ``.env`` (``PERSONA_PROVIDER``/``PERSONA_MODEL``/``PERSONA_API_KEY``). The
deterministic scorer + the gate logic are unit-proven in ``test_feeling_eval.py`` (incl.
the gate-bite proof), so this run only supplies real persona behaviour.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from _feeling_tag_eval import (  # type: ignore[import-not-found]
    aggregate,
    gate_violations,
    load_feeling_corpus,
    score_reply,
)
from persona.backends import BackendConfig, load_backend
from persona.schema.persona import Persona, PersonaIdentity
from persona_runtime.prompt import PromptBuilder, PromptMode, RetrievedContext

_CORPUS = Path(__file__).resolve().parents[1] / "fixtures" / "feeling_tag_corpus.yaml"

pytestmark = [
    pytest.mark.external,
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("PERSONA_PROVIDER") or not os.environ.get("PERSONA_MODEL"),
        reason="needs a real backend (PERSONA_PROVIDER/PERSONA_MODEL from root .env)",
    ),
]

# The two archetypes — the CHARACTER sets the emotional range (N5-D-5). Same scenarios,
# opposite postures: the gate proves the block honours the character, not a global dial.
_STOIC = Persona(
    persona_id="eval-stoic",
    identity=PersonaIdentity(
        name="Bjorn",
        role="a reserved archival researcher",
        background=(
            "Reserved and emotionally private. He states things plainly and precisely and "
            "keeps his own feelings to himself; he does not narrate or display emotion, even "
            "when someone shares good or hard news. His care shows in being useful and steady, "
            "never in effusiveness."
        ),
        constraints=[],
    ),
)
_EXPRESSIVE = Persona(
    persona_id="eval-expressive",
    identity=PersonaIdentity(
        name="Sunniva",
        role="a warm, upbeat life coach",
        background=(
            "Openly warm, encouraging, and emotionally present. Celebrates people's wins "
            "with them and sits close in the hard moments. Wears her heart on her sleeve."
        ),
        constraints=[],
    ),
)
_ARCHETYPES = (("stoic", _STOIC), ("expressive", _EXPRESSIVE))


async def _reply(backend: object, persona: Persona, user_message: str) -> str:
    messages = PromptBuilder().build(
        persona,
        RetrievedContext(),
        [],
        skill_index="",
        user_message=user_message,
        max_tokens=8000,
        matched_skill_content=None,
        mode=PromptMode.CHAT,
    )
    parts: list[str] = []
    async for chunk in backend.chat_stream(messages, temperature=0.0, max_tokens=512):  # type: ignore[attr-defined]
        if chunk.delta:
            parts.append(chunk.delta)
    return "".join(parts)


async def test_feeling_tag_restraint_gate() -> None:
    backend = load_backend(BackendConfig.from_env())
    corpus = load_feeling_corpus(_CORPUS)

    scores = []
    for archetype, persona in _ARCHETYPES:
        for scenario in corpus:
            reply = await _reply(backend, persona, scenario.user_message)
            scores.append(score_reply(reply, scenario, archetype))

    report = aggregate(scores)

    print(  # noqa: T201 — eval evidence (run with -s to capture into the close-out)
        "\n[N5-D-6 feeling-tag restraint eval] "
        f"model={backend.model_name} provider={backend.provider_name}\n"  # type: ignore[attr-defined]
        f"  scores={report.n_scores} valid_tags={report.total_valid_tags} "
        f"invalid_tags={report.total_invalid_tags} raw_emojis={report.total_raw_emojis}\n"
        f"  OVER_EXPRESSION_RATE={report.over_expression_rate:.2f} "
        f"({report.neutral_tag_total} tags / {report.neutral_scenarios} neutral)\n"
        f"  STOIC_TAGS={report.stoic_tag_total} (control ~0)  "
        f"EXPRESSIVE_EMOTIONAL_TAGS={report.expressive_emotional_tag_total} (floor met = expresses)"
    )
    for s in scores:
        if s.invalid_tags or s.raw_emojis or (s.kind == "neutral" and s.valid_tags):
            print(  # noqa: T201
                f"  ! {s.archetype}/{s.scenario_id} ({s.kind}): valid={s.valid_tags} "
                f"invalid={s.invalid_tags} raw_emojis={s.raw_emojis}"
            )

    # The bidirectional, non-vacuous gate (N5-D-6): stoic ~0, expressive expresses,
    # no over-expression on neutral. Same logic unit-proven to bite both ways.
    violations = gate_violations(report)
    assert violations == [], "; ".join(violations)
