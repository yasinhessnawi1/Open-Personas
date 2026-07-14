"""Unit tests for persona_runtime.prompt (T05; D-05-6, D-05-7, D-05-8)."""

# ruff: noqa: SLF001 — budget tests assert against the builder's private helpers.

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from persona.history import ConversationHistoryManager
from persona.schema.chunks import PersonaChunk
from persona.schema.conversation import Conversation, ConversationMessage
from persona.schema.persona import Persona, PersonaIdentity
from persona.skills import count_tokens
from persona_runtime.prompt import PromptBuilder, RetrievedContext


def _chunk(
    text: str, *, distance: float | None = None, meta: dict[str, str] | None = None
) -> PersonaChunk:
    return PersonaChunk(
        id=f"id-{abs(hash(text)) % 10000}",
        text=text,
        metadata=meta or {},
        distance=distance,
        created_at=datetime.now(UTC),
    )


def _persona(*, constraints: list[str] | None = None, tools: list[str] | None = None) -> Persona:
    return Persona(
        persona_id="astrid",
        identity=PersonaIdentity(
            name="Astrid",
            role="Norwegian tenancy law assistant",
            background="Knows husleieloven.",
            constraints=constraints if constraints is not None else ["Never give binding advice."],
        ),
        tools=tools if tools is not None else [],
    )


def _msg(role: str, content: str) -> ConversationMessage:
    return ConversationMessage(role=role, content=content, created_at=datetime.now(UTC))  # type: ignore[arg-type]


@pytest.fixture
def builder() -> PromptBuilder:
    return PromptBuilder()


class TestSectionOrdering:
    def test_system_block_has_sections_in_spec_order(self, builder: PromptBuilder) -> None:
        ctx = RetrievedContext(
            self_facts=[_chunk("I specialise in tenancy law.")],
            worldview=[_chunk("Tenants have strong protections.", meta={"epistemic": "fact"})],
            episodic=[_chunk("Last time we discussed mould.")],
        )
        msgs = builder.build(
            _persona(),
            ctx,
            history=[],
            skill_index="Available skills:\n- web_research",
            user_message="What are my rights?",
            max_tokens=8000,
            matched_skill_content="SKILL: do web research carefully.",
        )
        system = msgs[0].content
        # Each section appears, and in the spec §5.1 order.
        order_markers = [
            "You are Astrid",
            "You must NOT:",
            "Relevant facts about yourself:",
            "Your views:",
            "From earlier conversations:",
            "Available skills:",
            "SKILL: do web research",
            "Stay in character.",
        ]
        positions = [system.index(m) for m in order_markers]
        assert positions == sorted(positions), f"sections out of order: {positions}"

    def test_first_message_is_system_last_is_user(self, builder: PromptBuilder) -> None:
        msgs = builder.build(
            _persona(),
            RetrievedContext(),
            history=[_msg("user", "earlier"), _msg("assistant", "reply")],
            skill_index="",
            user_message="current question",
            max_tokens=8000,
        )
        assert msgs[0].role == "system"
        assert msgs[-1].role == "user"
        assert msgs[-1].content == "current question"
        # History sits between the system block and the current user message.
        assert [m.content for m in msgs[1:-1]] == ["earlier", "reply"]

    def test_worldview_epistemic_tag_in_parentheses(self, builder: PromptBuilder) -> None:
        ctx = RetrievedContext(
            worldview=[_chunk("ODR is usually preferable.", meta={"epistemic": "belief"})]
        )
        system = builder.build(_persona(), ctx, [], "", "q", max_tokens=8000)[0].content
        assert "ODR is usually preferable. (belief)" in system

    def test_empty_skill_index_omitted(self, builder: PromptBuilder) -> None:
        system = builder.build(_persona(), RetrievedContext(), [], "", "q", max_tokens=8000)[
            0
        ].content
        assert "Available skills" not in system

    def test_matched_skill_content_none_omits_section(self, builder: PromptBuilder) -> None:
        system = builder.build(
            _persona(),
            RetrievedContext(),
            [],
            "idx",
            "q",
            max_tokens=8000,
            matched_skill_content=None,
        )[0].content
        # Footer present, but no skill body beyond the index string itself.
        assert "Stay in character." in system


def _persona_lang(language_default: str) -> Persona:
    return Persona(
        persona_id="astrid",
        identity=PersonaIdentity(
            name="Astrid",
            role="Norwegian tenancy law assistant",
            background="Knows husleieloven.",
            language_default=language_default,
        ),
        tools=[],
    )


class TestReplyLanguageInjection:
    """Spec 32 B5 — the reply must be generated in the declared language."""

    def test_english_persona_gets_no_language_directive(self, builder: PromptBuilder) -> None:
        # English is the model default — no directive, so existing prompts are
        # unchanged (back-compat).
        system = builder.build(
            _persona_lang("en"), RetrievedContext(), [], "", "q", max_tokens=8000
        )[0].content
        assert "respond in" not in system.lower()

    def test_norwegian_persona_gets_norwegian_directive(self, builder: PromptBuilder) -> None:
        system = builder.build(
            _persona_lang("nb"), RetrievedContext(), [], "", "q", max_tokens=8000
        )[0].content
        assert "Norwegian" in system
        assert "respond in norwegian" in system.lower()

    def test_directive_sits_right_after_identity(self, builder: PromptBuilder) -> None:
        system = builder.build(
            _persona_lang("nb"), RetrievedContext(), [], "", "q", max_tokens=8000
        )[0].content.lower()
        assert system.index("you are astrid") < system.index("respond in norwegian")

    def test_reply_language_override_to_english_suppresses_directive(
        self, builder: PromptBuilder
    ) -> None:
        # Voice path: TTS fell back to English, so the reply must be English too —
        # no Norwegian directive even though the persona declares Norwegian.
        system = builder.build(
            _persona_lang("nb"),
            RetrievedContext(),
            [],
            "",
            "q",
            max_tokens=8000,
            reply_language="en",
        )[0].content
        assert "respond in" not in system.lower()

    def test_reply_language_override_wins_over_persona_default(
        self, builder: PromptBuilder
    ) -> None:
        system = builder.build(
            _persona_lang("en"),
            RetrievedContext(),
            [],
            "",
            "q",
            max_tokens=8000,
            reply_language="no",
        )[0].content
        assert "respond in norwegian" in system.lower()

    def test_unrecognized_language_no_directive(self, builder: PromptBuilder) -> None:
        # Fail-soft: an unservable language resolves to English → no directive.
        system = builder.build(
            _persona_lang("klingon"), RetrievedContext(), [], "", "q", max_tokens=8000
        )[0].content
        assert "respond in" not in system.lower()

    # ---------- Spec V14 D-V14-12: the reply-language MIRROR directive ----------

    def test_pin_is_the_default_text_chat_byte_identical(self, builder: PromptBuilder) -> None:
        """BINDING (D-V14-12): with ``reply_language_mirror`` UNSET (the default —
        every text-chat + Cartesia-voice caller), the directive is the PIN,
        byte-identical to the pre-V14 wording. The mirror wording never appears."""
        system = builder.build(
            _persona_lang("nb"), RetrievedContext(), [], "", "q", max_tokens=8000
        )[0].content
        assert (
            "Always respond in Norwegian, regardless of the language the user "
            "writes or speaks in. Every reply must be written in Norwegian." in system
        )
        assert "same language the user writes" not in system.lower()

    def test_mirror_mode_produces_mirror_directive_for_non_english(
        self, builder: PromptBuilder
    ) -> None:
        """mirror=True (utterance-level TTS): reply in the user's language,
        default to the persona's declared language — NOT the pin."""
        system = builder.build(
            _persona_lang("nb"),
            RetrievedContext(),
            [],
            "",
            "q",
            max_tokens=8000,
            reply_language_mirror=True,
        )[0].content
        lowered = system.lower()
        assert "same language the user writes or speaks in" in lowered
        assert "reply in norwegian" in lowered  # the default/fallback language
        assert "always respond in" not in lowered  # NOT the pin

    def test_mirror_mode_english_persona_no_directive(self, builder: PromptBuilder) -> None:
        """mirror=True + English default → no directive (mirroring + English are
        both the model default; byte-identical to no-directive)."""
        system = builder.build(
            _persona_lang("en"),
            RetrievedContext(),
            [],
            "",
            "q",
            max_tokens=8000,
            reply_language_mirror=True,
        )[0].content
        lowered = system.lower()
        assert "respond in" not in lowered
        assert "same language" not in lowered


class TestIdentityFloor:
    def test_identity_and_constraints_survive_budget_reduction(
        self, builder: PromptBuilder
    ) -> None:
        # Give a tiny budget so reduction fires; identity + constraints must remain.
        ctx = RetrievedContext(
            self_facts=[_chunk("fact " * 50)],
            worldview=[_chunk("view " * 50)],
            episodic=[_chunk("episode " * 50)],
        )
        msgs = builder.build(
            _persona(constraints=["Never give binding advice."]),
            ctx,
            history=[],
            skill_index="Available skills:\n- web_research",
            user_message="hi",
            max_tokens=80,  # forces dropping retrieved context
        )
        system = msgs[0].content
        assert "You are Astrid" in system
        assert "You must NOT:" in system
        assert "Available skills:" in system


class TestContextBudgetReduction:
    def test_episodic_dropped_first(self, builder: PromptBuilder) -> None:
        # Budget that fits identity+constraints+self_facts+worldview but not episodic.
        big = "word " * 200
        ctx = RetrievedContext(
            self_facts=[_chunk("a short fact")],
            worldview=[_chunk("a short view", meta={"epistemic": "fact"})],
            episodic=[_chunk(big)],
        )
        # Pick a budget above the no-episodic size but below the with-episodic size.
        no_ep = builder.build(
            _persona(), ctx.model_copy(update={"episodic": []}), [], "", "q", max_tokens=100000
        )
        budget = builder._token_total(no_ep) + 5
        msgs = builder.build(_persona(), ctx, [], "", "q", max_tokens=budget)
        system = msgs[0].content
        assert "a short fact" in system  # self_facts kept
        assert "a short view" in system  # worldview kept
        assert big.strip() not in system  # episodic dropped

    def test_reduction_order_episodic_then_worldview_then_self_facts(
        self, builder: PromptBuilder
    ) -> None:
        ctx = RetrievedContext(
            self_facts=[_chunk("SELFFACT")],
            worldview=[_chunk("WORLDVIEW", meta={"epistemic": "fact"})],
            episodic=[_chunk("EPISODIC")],
        )
        # T15 widened _reductions to return tuples of
        # (RetrievedContext, DocumentContext | None). Without documents, the
        # ladder still has 3 stages with the same RetrievedContext semantics.
        stages = builder._reductions(ctx)
        assert len(stages) == 3
        # Stage 1: only episodic dropped.
        assert stages[0][0].episodic == []
        assert stages[0][0].worldview != []
        # Stage 2: episodic + worldview dropped, self_facts kept.
        assert stages[1][0].episodic == []
        assert stages[1][0].worldview == []
        assert stages[1][0].self_facts != []
        # Stage 3: all retrieved context cleared.
        assert stages[2][0].self_facts == []
        # DocumentContext slot rides through unchanged.
        for _, doc_ctx in stages:
            assert doc_ctx is None


class TestAcceptance12ContextWindow:
    """Acceptance #12: a 30-turn conversation stays within an 8K mid-tier window.

    Per Phase 1 steer #7: construct a 30-turn Conversation directly and assert
    the rendered prompt token count is < 8000 after history-manage + prompt-build.
    No 30 mock round-trips.
    """

    def test_30_turn_prompt_under_8000_tokens(self, builder: PromptBuilder) -> None:
        manager = ConversationHistoryManager(compact_every=10, keep_recent=5)
        # 30 turns of realistic-length messages.
        messages = []
        for i in range(30):
            role = "user" if i % 2 == 0 else "assistant"
            text = (
                f"Turn {i}: this is a reasonably sized conversational message about "
                f"Norwegian tenancy law, deposits, and dispute resolution procedures."
            )
            messages.append(
                ConversationMessage(role=role, content=text, created_at=datetime.now(UTC))
            )  # type: ignore[arg-type]
        conv = Conversation(conversation_id="c30", persona_id="astrid", messages=messages)

        # The loop pre-computes the summary; here we simulate a compact summary.
        history = manager.manage(
            conv, summariser=lambda _msgs: "Summary of earlier turns about tenancy law."
        )

        ctx = RetrievedContext(
            self_facts=[_chunk("I specialise in husleieloven.")],
            worldview=[_chunk("Tenants have strong protections.", meta={"epistemic": "fact"})],
            episodic=[_chunk("Earlier we discussed mould complaints.")],
        )
        prompt = builder.build(
            _persona(),
            ctx,
            history,
            skill_index="Available skills:\n- web_research\n- document_drafting",
            user_message="So what should I do about my deposit?",
            max_tokens=8000,
        )
        total = sum(count_tokens(m.content) for m in prompt)
        assert total < 8000, f"30-turn prompt was {total} tokens (expected < 8000)"


class TestProducedFilesVerification:
    """D-19-X-prompt-builder-produced-files-verification (chain entry 13).

    Capability-gated provider-agnostic instruction teaching the model to
    end every ``code_execution`` call with ``os.listdir("/workspace/out")``,
    never fabricate save confirmations, and reconcile reported paths
    against the listdir output before claiming success.
    """

    def test_block_emitted_when_code_execution_in_tools(self, builder: PromptBuilder) -> None:
        system = builder.build(
            _persona(tools=["code_execution"]),
            RetrievedContext(),
            history=[],
            skill_index="",
            user_message="q",
            max_tokens=8000,
        )[0].content
        assert "code_execution" in system
        assert 'os.listdir("/workspace/out")' in system
        assert "Never fabricate" in system
        assert "match every file path" in system

    def test_block_omitted_when_code_execution_not_in_tools(self, builder: PromptBuilder) -> None:
        system = builder.build(
            _persona(tools=[]),
            RetrievedContext(),
            history=[],
            skill_index="",
            user_message="q",
            max_tokens=8000,
        )[0].content
        assert 'os.listdir("/workspace/out")' not in system
        assert "Never fabricate" not in system

    def test_block_omitted_for_unrelated_tools(self, builder: PromptBuilder) -> None:
        system = builder.build(
            _persona(tools=["web_search", "use_skill"]),
            RetrievedContext(),
            history=[],
            skill_index="",
            user_message="q",
            max_tokens=8000,
        )[0].content
        assert 'os.listdir("/workspace/out")' not in system

    def test_block_appears_before_footer(self, builder: PromptBuilder) -> None:
        system = builder.build(
            _persona(tools=["code_execution"]),
            RetrievedContext(),
            history=[],
            skill_index="",
            user_message="q",
            max_tokens=8000,
        )[0].content
        listdir_pos = system.index('os.listdir("/workspace/out")')
        footer_pos = system.index("Stay in character.")
        assert listdir_pos < footer_pos, (
            "produced-files verification block must come before the footer"
        )
        # And the footer remains the final non-empty line of the system block.
        assert system.rstrip().endswith("Stay in character. Cite sources when using tool results.")


class TestFileWorkspaceConventions:
    """File-workspace conventions block — capability-gated.

    Teaches the persistent working dir + the file_write↔code_execution
    bridge (intermediate/) so the model uses files correctly even in a task
    run with NO attached documents. Renders for any persona whose tools
    include file_read, file_write, or code_execution.
    """

    def test_block_emitted_when_file_read_in_tools(self, builder: PromptBuilder) -> None:
        system = builder.build(
            _persona(tools=["file_read"]),
            RetrievedContext(),
            history=[],
            skill_index="",
            user_message="q",
            max_tokens=8000,
        )[0].content
        assert "persists across the task" in system
        assert "intermediate/" in system
        assert "file_read" in system

    def test_block_emitted_when_code_execution_in_tools(self, builder: PromptBuilder) -> None:
        system = builder.build(
            _persona(tools=["code_execution"]),
            RetrievedContext(),
            history=[],
            skill_index="",
            user_message="q",
            max_tokens=8000,
        )[0].content
        assert "persists across the task" in system
        assert "intermediate/" in system
        assert "file_read" in system

    def test_block_omitted_when_no_file_tools(self, builder: PromptBuilder) -> None:
        system = builder.build(
            _persona(tools=["web_search", "use_skill"]),
            RetrievedContext(),
            history=[],
            skill_index="",
            user_message="q",
            max_tokens=8000,
        )[0].content
        assert "persists across the task" not in system

    def test_block_appears_before_footer(self, builder: PromptBuilder) -> None:
        system = builder.build(
            _persona(tools=["file_read", "code_execution"]),
            RetrievedContext(),
            history=[],
            skill_index="",
            user_message="q",
            max_tokens=8000,
        )[0].content
        conventions_pos = system.index("persists across the task")
        footer_pos = system.index("Stay in character.")
        assert conventions_pos < footer_pos


class TestMcpSearchGuidance:
    """The N4 gap-detection prompt block — capability-gated on ``mcp_search``."""

    def _system(self, builder: PromptBuilder, *, tools: list[str]) -> str:
        return builder.build(
            _persona(tools=tools),
            RetrievedContext(),
            history=[],
            skill_index="",
            user_message="q",
            max_tokens=8000,
        )[0].content

    def test_guidance_present_when_mcp_search_granted(self, builder: PromptBuilder) -> None:
        system = self._system(builder, tools=["mcp_search"])
        assert "mcp_search" in system
        # the in-role bound + propose-don't-act posture is the load-bearing content
        assert "in-role" in system.lower() or "within your role" in system.lower()
        assert "propose" in system.lower()

    def test_guidance_absent_without_mcp_search(self, builder: PromptBuilder) -> None:
        system = self._system(builder, tools=["web_search"])
        assert "mcp_search" not in system

    def test_guidance_before_footer(self, builder: PromptBuilder) -> None:
        system = self._system(builder, tools=["mcp_search"])
        assert system.index("mcp_search") < system.index("Stay in character.")
