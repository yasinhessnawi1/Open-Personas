"""The voice call's FIRST message is memory-aware (R9-002).

World-b fix: the live voice composition never wired the K9 ``core_block_provider``
(it stayed ``None`` unconditionally in ``build_agent_session``), so the greeting
turn — like every voice turn — assembled with no core-memory block. The fix reads
the block ONCE at session setup (``_load_core_block``, the ``_load_user_name``
pattern: off the per-utterance path, fail-soft) and serves it through the
EXISTING prompt seam; the enhanced ``_GREETING_NUDGE`` instructs a guarded
continuity branch over whatever reached the context — the model decides from
what it sees, the code decides what reaches the context.

These tests drive the REAL trigger chain: the runner's real nudge through the
real ``VoicePromptAssembler`` → shared ``PromptBuilder`` (no voice fork), and the
real ``_load_core_block`` seam (gate + fail-soft + absent-block).
"""

# ruff: noqa: ARG001, ARG002, ARG005 — store/seam doubles keep the real
# collaborators' signatures without consuming every argument.

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import persona.recall.core_memory as core_memory
from persona.backends import BackendConfig
from persona.history import ConversationHistoryManager
from persona.recall.core_memory import CoreBlock
from persona.schema.chunks import PersonaChunk
from persona.schema.conversation import Conversation
from persona.schema.persona import Persona, PersonaIdentity
from persona_runtime.prompt import PromptBuilder
from persona_runtime.router import Router
from persona_runtime.tier import TierConfig, TierRegistry
from persona_voice.agent.runner import _GREETING_NUDGE, _greeting_transcript, _load_core_block
from persona_voice.model import VoicePromptAssembler, VoiceTurnContext

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import pytest
    from persona.stores.embedder import Embedder
    from sqlalchemy import Engine

# The PromptBuilder's core-memory header (K9 T7). Literal on purpose: if the
# rendered contract drifts, this suite must fail visibly, not silently pass.
_CORE_HEADER = "What you carry about this person across your time together:"


# --- the nudge contract (deliverable a + b: branch structure, not model output) ---


def test_greeting_nudge_carries_the_guarded_continuity_branch() -> None:
    """With shared history in context, the nudge steers a returning-acquaintance
    greeting bounded to ONE light/neutral open thread (the R9-002 ask)."""
    assert "shared history" in _GREETING_NUDGE
    assert "returning acquaintance" in _GREETING_NUDGE
    assert "at most one recent, light, neutral open thread" in _GREETING_NUDGE


def test_greeting_nudge_never_raises_sensitive_topics_unprompted() -> None:
    """The K4 wellbeing guard, instruction-level (defence in depth over the
    K4-composed upstream content): every sensitive category is named, the
    prohibition is on UNPROMPTED raising, and doubt degrades to a plain hello."""
    for topic in ("health", "crisis", "relationships", "finances", "legal"):
        assert topic in _GREETING_NUDGE
    assert "Never raise sensitive topics" in _GREETING_NUDGE
    assert "unprompted" in _GREETING_NUDGE
    assert "plain warm hello" in _GREETING_NUDGE


def test_greeting_nudge_keeps_the_plain_hello_branch_for_no_history() -> None:
    """Cold start (first-ever call / empty memory / provider ``None`` / fetch
    failed): the instruction explicitly branches to today's behaviour — one
    short warm hello."""
    assert "no shared history" in _GREETING_NUDGE
    assert "one short warm hello" in _GREETING_NUDGE


def test_greeting_transcript_is_still_synthetic_and_carries_the_nudge() -> None:
    """R9-001 interplay: the enhanced nudge still rides the synthetic turn-0
    transcript, so it never persists to the durable transcript or memory."""
    t = _greeting_transcript()
    assert t.synthetic is True
    assert t.is_final is True
    assert t.text == _GREETING_NUDGE


# --- the assembled greeting prompt (the real chain: nudge → assembler → builder) ---


class _FakeStore:
    def __init__(self, all_chunks: list[PersonaChunk] | None = None) -> None:
        self._all = all_chunks or []

    def query(
        self, persona_id: str, query: str, top_k: int, **filters: object
    ) -> list[PersonaChunk]:
        return []

    def get_all(self, persona_id: str, *, include_superseded: bool = False) -> list[PersonaChunk]:
        return list(self._all)

    def recent(self, persona_id: str, limit: int) -> list[PersonaChunk]:
        return []


def _ctx(*, core_block_provider: Callable[[], str | None] | None) -> VoiceTurnContext:
    ident = PersonaChunk(id="i1", text="I am Astrid.", metadata={}, created_at=datetime.now(UTC))
    stores = {k: _FakeStore() for k in ("self_facts", "worldview", "episodic")}
    stores["identity"] = _FakeStore(all_chunks=[ident])
    cfg = BackendConfig(provider="anthropic", model="m", api_key=None)  # type: ignore[arg-type]
    return VoiceTurnContext(
        persona=Persona(
            persona_id="astrid",
            identity=PersonaIdentity(name="Astrid", role="assistant", background="bg"),
        ),
        stores=stores,  # type: ignore[arg-type]
        conversation=Conversation(conversation_id="c1", persona_id="astrid", messages=[]),
        prompt_builder=PromptBuilder(),
        router=Router(),
        tier_registry=TierRegistry({"frontier": TierConfig(name="frontier", backend_config=cfg)}),
        history_manager=ConversationHistoryManager(compact_every=10, keep_recent=5),
        core_block_provider=core_block_provider,
    )


def _greeting_system(core_block_provider: Callable[[], str | None] | None) -> str:
    """Assemble turn 0 exactly as production does: the runner's REAL nudge as the
    user message, empty history, through the real assembler + shared builder."""
    assembler = VoicePromptAssembler(_ctx(core_block_provider=core_block_provider))
    msgs = assembler.build(_GREETING_NUDGE, history=[], max_tokens=8000)
    content = msgs[0].content
    assert isinstance(content, str)
    # The nudge itself is the turn's user message (the producer path).
    tail = msgs[-1].content
    assert tail == _GREETING_NUDGE
    return content


def test_greeting_prompt_carries_the_head_start_when_history_exists() -> None:
    """Provider wired (the setup read found a block) ⇒ the greeting turn's
    system prompt carries the core-memory block — the head start the continuity
    branch acts on."""
    system = _greeting_system(lambda: "Alex is restoring an old sailboat with their brother.")
    assert _CORE_HEADER in system
    assert "restoring an old sailboat" in system


def test_greeting_prompt_without_history_is_byte_identical() -> None:
    """Provider ``None`` (cold start / gate off / fetch failed) ⇒ no core block
    renders and the prompt is byte-identical minus exactly that block — the
    plain-hello branch is what remains actionable."""
    baseline = _greeting_system(None)
    assert _CORE_HEADER not in baseline
    block = "Alex is restoring an old sailboat with their brother."
    with_head_start = _greeting_system(lambda: block)
    assert with_head_start.replace(f"\n\n{_CORE_HEADER}\n{block}", "", 1) == baseline


# --- the session-setup fetch seam (deliverable c: fail-soft, gate, absent block) ---


def _run_load(tmp_path: Path) -> str | None:
    """Invoke the real ``_load_core_block`` with inert engine/embedder doubles.

    ``PostgresBackend.__init__`` stores refs without touching the engine, so the
    only engine contact is the store read — which these tests intercept at the
    ``read_core_block`` seam (the function's real collaborator).
    """
    return _load_core_block(
        cast("Engine", object()), cast("Embedder", object()), "astrid", tmp_path
    )


def test_setup_core_block_read_returns_the_current_block_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PERSONA_RECALL_CORE_ENABLED", "true")
    block = CoreBlock(text="Alex is restoring an old sailboat.", source_ids=("s1",))
    monkeypatch.setattr(core_memory, "read_core_block", lambda store, persona_id: block)
    assert _run_load(tmp_path) == "Alex is restoring an old sailboat."


def test_setup_core_block_read_is_fail_soft_on_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Any store/read error ⇒ ``None`` ⇒ no head start ⇒ the plain-hello branch
    — the head start is a nicety and must never break a call."""

    monkeypatch.setenv("PERSONA_RECALL_CORE_ENABLED", "true")

    def _boom(store: object, persona_id: str) -> CoreBlock | None:
        msg = "db down"
        raise RuntimeError(msg)

    monkeypatch.setattr(core_memory, "read_core_block", _boom)
    assert _run_load(tmp_path) is None


def test_setup_core_block_read_returns_none_before_first_refresh(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PERSONA_RECALL_CORE_ENABLED", "true")
    monkeypatch.setattr(core_memory, "read_core_block", lambda store, persona_id: None)
    assert _run_load(tmp_path) is None


def test_setup_core_block_read_respects_the_core_enabled_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``PERSONA_RECALL_CORE_ENABLED=false`` (the SAME K9 gate chat's provider
    honours) ⇒ ``None`` without ever reaching the store."""
    monkeypatch.setenv("PERSONA_RECALL_CORE_ENABLED", "false")

    def _must_not_be_called(store: object, persona_id: str) -> CoreBlock | None:
        msg = "store read must not run when the gate is off"
        raise AssertionError(msg)

    monkeypatch.setattr(core_memory, "read_core_block", _must_not_be_called)
    assert _run_load(tmp_path) is None
