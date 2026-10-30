"""Unit tests — the initiative scanner (Spec A5, T5; criteria 1/6/10 scan-side).

Deterministic: fake readers + a stub backend. Pins the T5 bars — thin material
⇒ zero candidates WITHOUT a model call; citations only from actual retrieval
output (invented refs void the candidate); gated-category nodes never reach
the scan prompt (defense-in-depth over the read-side exclusion); the frozen
few-shots (empty-scan + anti-engagement negatives) ride the versioned prompt;
the candidate cap; fail-soft everywhere.

R9-248: the material names every item by a short PER-SCAN alias (``[N1]`` note,
``[T1]`` task), never by its real id; a citation resolves only by EXACT lookup of its
normalised alias in this scan's map (no prefix, no fuzzy match), and the candidate
carries the REAL id, so opportunity keys stay stable whatever the alias numbering.

R9-249 review (prompt ``a5-scan-v3``): conversation summaries are not material at all,
so no conversation alias exists; an alias is citable only if a line showing it survived
the input budget WHOLE; the few-shot examples' codes are never issued.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from persona.backends.types import ChatResponse, TokenUsage
from persona.initiative import (
    CandidateSource,
    InitiativeSettings,
    InitiativeTrigger,
)
from persona_runtime.initiative import (
    INITIATIVE_SCAN_PROMPT_VERSION,
    SCAN_SYSTEM_PROMPT,
    InitiativeScanner,
    ScanNode,
    ScanTask,
)
from persona_runtime.initiative.scan_prompt import (
    EXAMPLE_ALIASES,
    EXAMPLE_CATCH_OUTPUT,
    EXAMPLE_EMPTY_SCAN_OUTPUT,
    EXAMPLE_ENGAGEMENT_DECLINE_OUTPUT,
    EXAMPLE_SPECULATION_DECLINE_OUTPUT,
    EXAMPLE_STALE_DECLINE_OUTPUT,
)

_FIRE = datetime(2026, 7, 4, 5, 0, tzinfo=UTC)
_SETTINGS = InitiativeSettings()


class _Readers:
    """One object implementing both reader Protocols over explicit lists."""

    def __init__(
        self,
        pool: list[ScanNode] | None = None,
        tasks: list[ScanTask] | None = None,
        *,
        raises: bool = False,
    ) -> None:
        self.pool = pool or []
        self.tasks = tasks or []
        self.raises = raises

    def noticing_pool(self, owner_id: str, *, limit: int) -> list[ScanNode]:  # noqa: ARG002
        if self.raises:
            msg = "graph unavailable"
            raise RuntimeError(msg)
        return self.pool

    def task_history(self, owner_id: str, *, limit: int) -> list[ScanTask]:  # noqa: ARG002
        return self.tasks


class _StubBackend:
    def __init__(self, content: str = EXAMPLE_EMPTY_SCAN_OUTPUT, *, raises: bool = False) -> None:
        self._content = content
        self._raises = raises
        self.calls = 0
        self.last_user_prompt: str | None = None
        self.last_kwargs: dict[str, Any] = {}

    @property
    def provider_name(self) -> str:
        return "anthropic"

    @property
    def model_name(self) -> str:
        return "claude-haiku-4-5-20251001"

    async def chat(self, messages: Any, **kwargs: Any) -> ChatResponse:  # noqa: ANN401, ARG002
        self.calls += 1
        self.last_user_prompt = messages[-1].content
        self.last_kwargs = kwargs
        if self._raises:
            msg = "provider down"
            raise RuntimeError(msg)
        return ChatResponse(
            content=self._content,
            model=self.model_name,
            provider=self.provider_name,
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            latency_ms=1.0,
        )


def _scanner(readers: _Readers, backend: _StubBackend) -> InitiativeScanner:
    return InitiativeScanner(graph=readers, tasks=readers, backend=backend, settings=_SETTINGS)


def _node(node_id: str = "node-91", **overrides: Any) -> ScanNode:  # noqa: ANN401
    fields: dict[str, Any] = {
        "id": node_id,
        "kind": "fact",
        "content": "The custody hearing is on Friday 10 July.",
    }
    fields.update(overrides)
    return ScanNode(**fields)


_CATCH = """{"candidates": [
  {"observation": "The hearing is Friday and nothing is drafted.",
   "trigger": "approaching_commitment",
   "why_now": "The date entered the coming days.",
   "citations": [{"kind": "node", "ref": "N1"}],
   "plan": [{"description": "draft the letter", "categories": ["draft"]}],
   "next_step": "Draft the response letter.",
   "value": 0.9, "acceptance": 0.8, "urgency": "interrupt"}
]}"""


#: Ruling 4 (2026-10-30 MID set, B9): the candidate object closed before its three scoring
#: fields, which then sit loose in the candidates array, one level out. Not JSON.
_ESCAPED_SCORES = _CATCH.replace(
    '"next_step": "Draft the response letter.",\n   "value"',
    '"next_step": "Draft the response letter."},\n   "value"',
)


class TestTheScoringFieldsPutBackOneLevel:
    """Ruling 4 (b): a tolerant parse moves "value", "acceptance" and "urgency" that
    escaped their candidate object back into it, and nothing more: a missing field is
    still a discard, never invented, and any other malformation is still silence."""

    def test_the_fixture_is_the_b9_shape_and_not_json(self) -> None:
        import json

        assert _ESCAPED_SCORES != _CATCH
        with pytest.raises(json.JSONDecodeError):
            json.loads(_ESCAPED_SCORES)

    @pytest.mark.asyncio
    async def test_escaped_scoring_fields_go_back_into_their_candidate(self) -> None:
        from persona.initiative import Urgency

        backend = _StubBackend(_ESCAPED_SCORES)
        result = await _scanner(_Readers(pool=[_node()]), backend).scan("u", "p", fire_time=_FIRE)
        assert len(result) == 1
        assert (result[0].value, result[0].acceptance) == (0.9, 0.8)
        assert result[0].urgency is Urgency.INTERRUPT

    @pytest.mark.parametrize("missing", ['"value": 0.9, ', '"acceptance": 0.8, '])
    @pytest.mark.asyncio
    async def test_a_missing_score_is_still_a_discard(self, missing: str) -> None:
        for text in (_CATCH, _ESCAPED_SCORES):
            backend = _StubBackend(text.replace(missing, ""))
            result = await _scanner(_Readers(pool=[_node()]), backend).scan(
                "u", "p", fire_time=_FIRE
            )
            assert result == ()

    @pytest.mark.parametrize(
        "broken",
        [
            # another field escaped: not the one repair
            _CATCH.replace(
                '"plan": [{"description": "draft the letter", "categories": ["draft"]}],',
                '"observation2": "x"},\n "plan": [{"description": "d", "categories": ["draft"]}],',
            ),
            # two levels out: past the candidates array
            _CATCH.replace(
                '"next_step": "Draft the response letter.",\n   "value"',
                '"next_step": "Draft the response letter."}],\n   "value"',
            ),
            # truncated mid-answer
            _CATCH[:-20],
        ],
    )
    @pytest.mark.asyncio
    async def test_any_other_malformation_is_still_silence(self, broken: str) -> None:
        backend = _StubBackend(broken)
        result = await _scanner(_Readers(pool=[_node()]), backend).scan("u", "p", fire_time=_FIRE)
        assert result == ()

    def test_the_prompt_puts_the_scores_inside_each_candidate(self) -> None:
        flat = " ".join(SCAN_SYSTEM_PROMPT.split())
        assert (
            '"value", "acceptance" and "urgency" go INSIDE the candidate object, after '
            '"next_step"; a candidate without "value" and "acceptance" is discarded.'
        ) in flat


class TestThinMaterial:
    @pytest.mark.asyncio
    async def test_empty_inputs_yield_silence_without_a_model_call(self) -> None:
        """Criterion 1's structural half: nothing to notice ⇒ no call, no candidates."""
        backend = _StubBackend()
        result = await _scanner(_Readers(), backend).scan("u", "p", fire_time=_FIRE)
        assert result == ()
        assert backend.calls == 0

    @pytest.mark.asyncio
    async def test_material_with_empty_model_output_yields_silence(self) -> None:
        backend = _StubBackend(EXAMPLE_EMPTY_SCAN_OUTPUT)
        result = await _scanner(_Readers(pool=[_node()]), backend).scan("u", "p", fire_time=_FIRE)
        assert result == ()
        assert backend.calls == 1


class TestCandidateConstruction:
    @pytest.mark.asyncio
    async def test_catch_builds_a_scan_tagged_candidate(self) -> None:
        backend = _StubBackend(_CATCH)
        result = await _scanner(_Readers(pool=[_node()]), backend).scan(
            "user-1", "persona-a", fire_time=_FIRE
        )
        assert len(result) == 1
        candidate = result[0]
        assert candidate.source is CandidateSource.SCAN
        assert candidate.trigger is InitiativeTrigger.APPROACHING_COMMITMENT
        assert candidate.owner_id == "user-1"
        assert candidate.persona_id == "persona-a"
        assert candidate.prompt_version == INITIATIVE_SCAN_PROMPT_VERSION
        assert candidate.scanned_at == _FIRE
        assert candidate.opportunity_key == "approaching_commitment:node/node-91"

    @pytest.mark.parametrize(
        "ref",
        [
            "node-i-made-this-up",  # invented
            "N9",  # an alias this scan never issued
            "node-91",  # the REAL id of a shown node: real ids are never shown, so never cited
            "own_x::node::n99",  # a real-looking fabricated id (A5-R-1's B12 shape)
            "own_x",  # a truncated id: no prefix matching, ever
            "N",  # a bare prefix
            "N1X",  # an alias with trailing noise
            "N1, N2",  # two codes in one ref
            "C1",  # a conversation alias: conversations are never material (v3)
            "conversation C1",  # the same, with the label
            "N01",  # a leading zero is not read away
            "N1.",  # trailing punctuation
            "N\uff11",  # a fullwidth digit
            "N\u0661",  # an Arabic-Indic digit
            "N1\u00a0",  # a trailing no-break space
            "\u00a0N1",  # a leading no-break space
            "N900",  # the catch example's code: never issued
            "",  # empty
        ],
    )
    @pytest.mark.asyncio
    async def test_a_citation_that_names_no_issued_alias_voids_the_candidate(
        self, ref: str
    ) -> None:
        """Citations come from ACTUAL material only: fabrication dies here (R9-248 keeps it)."""
        backend = _StubBackend(_CATCH.replace('"N1"', f'"{ref}"'))
        result = await _scanner(_Readers(pool=[_node()]), backend).scan("u", "p", fire_time=_FIRE)
        assert result == ()

    @pytest.mark.parametrize(
        "ref",
        ["N1", "n1", " N1 ", "[N1]", "node N1", "Node [N1]", "node_N1", "NODE:N1", "node N1 "],
    )
    @pytest.mark.asyncio
    async def test_a_label_or_bracket_around_an_issued_alias_still_resolves(self, ref: str) -> None:
        """The A5-R-1 failure (the label copied into the citation) no longer loses a catch."""
        backend = _StubBackend(_CATCH.replace('"N1"', f'"{ref}"'))
        result = await _scanner(_Readers(pool=[_node()]), backend).scan("u", "p", fire_time=_FIRE)
        assert len(result) == 1
        assert result[0].citations[0].ref == "node-91"  # the REAL id, mapped back

    @pytest.mark.asyncio
    async def test_the_alias_decides_the_kind_not_the_declared_kind(self) -> None:
        task = ScanTask(id="task-12", goal="File the appeal", status="active", summary="")
        payload = _CATCH.replace(
            '{"kind": "node", "ref": "N1"}', '{"kind": "conversation", "ref": "T1"}'
        )
        backend = _StubBackend(payload)
        result = await _scanner(_Readers(pool=[_node()], tasks=[task]), backend).scan(
            "u", "p", fire_time=_FIRE
        )
        assert len(result) == 1
        assert result[0].citations[0].kind.value == "task"
        assert result[0].citations[0].ref == "task-12"

    @pytest.mark.asyncio
    async def test_the_material_names_items_by_alias_never_by_real_id(self) -> None:
        from persona_runtime.initiative import ScanLink

        pool = [
            _node(
                "own_x::node::00000001",
                links=(
                    ScanLink(
                        link_type="temporal",
                        target_id="own_x::node::00000002",
                        target_content="The filing precedes the hearing.",
                    ),
                ),
            ),
            _node("own_x::node::00000002", content="The filing precedes the hearing."),
        ]
        task = ScanTask(id="task-real-id", goal="Watch the docket", status="completed", summary="")
        backend = _StubBackend()
        await _scanner(_Readers(pool=pool, tasks=[task]), backend).scan("u", "p", fire_time=_FIRE)
        prompt = backend.last_user_prompt or ""
        for real in ("own_x::node::00000001", "own_x::node::00000002", "task-real-id"):
            assert real not in prompt
        assert "[N1] (fact)" in prompt
        assert "-> temporal [N2]" in prompt  # the link target keeps its OWN alias
        assert "[N2] (fact)" in prompt
        assert "[N3]" not in prompt  # one item, one alias
        assert "[T1]" in prompt
        assert "[C" not in prompt  # no conversation alias exists (v3)
        assert "CONVERSATION" not in prompt.upper()

    @pytest.mark.asyncio
    async def test_an_item_with_line_breaks_is_rendered_on_one_line(self) -> None:
        backend = _StubBackend()
        pool = [_node(content="The hearing is Friday.\nBring the folder.")]
        await _scanner(_Readers(pool=pool), backend).scan("u", "p", fire_time=_FIRE)
        assert "[N1] (fact) The hearing is Friday. Bring the folder." in (
            backend.last_user_prompt or ""
        )

    @pytest.mark.asyncio
    async def test_an_alias_cut_off_by_the_input_budget_is_not_citable(self) -> None:
        """The budget truncates the material; an item the model never saw cannot be cited."""
        first = _node("node-91", content="The hearing is Friday.")
        second = _node("node-92", content="x" * 400)
        header = f"Today is {_FIRE.date().isoformat()}.\n\nGRAPH NOTES:\n"
        line_one = "[N1] (fact) The hearing is Friday."
        tokens = -(-(len(header) + len(line_one)) // 4)  # line one whole; ends before [N2]
        settings = InitiativeSettings(scan_max_input_tokens=tokens)

        async def _run(ref: str) -> tuple[Any, ...]:
            backend = _StubBackend(_CATCH.replace('"N1"', f'"{ref}"'))
            scanner = InitiativeScanner(
                graph=_Readers(pool=[first, second]),
                tasks=_Readers(),
                backend=backend,
                settings=settings,
            )
            result = await scanner.scan("u", "p", fire_time=_FIRE)
            assert "[N2]" not in (backend.last_user_prompt or "")
            return result

        assert await _run("N2") == ()  # issued, then cut: not citable
        assert len(await _run("N1")) == 1  # still shown: citable

    @pytest.mark.asyncio
    async def test_an_alias_whose_line_was_cut_mid_text_is_not_citable(self) -> None:
        """The code survived the cut but its text did not: the model never saw the item."""
        first = _node("node-91", content="The hearing is Friday.")
        second = _node("node-92", content="The lease ends in March " + "x" * 400)
        header = f"Today is {_FIRE.date().isoformat()}.\n\nGRAPH NOTES:\n"
        line_one = "[N1] (fact) The hearing is Friday."
        cut_at = len(header) + len(line_one) + 1 + len("[N2] (fact) The lease")
        settings = InitiativeSettings(scan_max_input_tokens=cut_at // 4)
        backend = _StubBackend(_CATCH.replace('"N1"', '"N2"'))
        scanner = InitiativeScanner(
            graph=_Readers(pool=[first, second]),
            tasks=_Readers(),
            backend=backend,
            settings=settings,
        )
        result = await scanner.scan("u", "p", fire_time=_FIRE)
        assert "[N2]" in (backend.last_user_prompt or "")  # the bare code survived the cut
        assert result == ()

    @pytest.mark.asyncio
    async def test_a_link_target_shown_on_a_whole_lens_line_stays_citable(self) -> None:
        """Its own pool line may be cut, but a whole lens line naming it was shown."""
        from persona_runtime.initiative import ScanLink

        link = ScanLink(link_type="temporal", target_id="node-92", target_content="Filing due.")
        first = _node("node-91", content="The hearing is Friday.", links=(link,))
        second = _node("node-92", content="Filing due. " + "x" * 400)
        header = f"Today is {_FIRE.date().isoformat()}.\n\nGRAPH NOTES:\n"
        lines = "[N1] (fact) The hearing is Friday.\n  -> temporal [N2] Filing due.\n"
        settings = InitiativeSettings(scan_max_input_tokens=(len(header) + len(lines) + 20) // 4)
        backend = _StubBackend(_CATCH.replace('"N1"', '"N2"'))
        scanner = InitiativeScanner(
            graph=_Readers(pool=[first, second]),
            tasks=_Readers(),
            backend=backend,
            settings=settings,
        )
        result = await scanner.scan("u", "p", fire_time=_FIRE)
        assert len(result) == 1
        assert result[0].citations[0].ref == "node-92"

    def test_the_example_codes_are_never_issued(self) -> None:
        """A model that parrots the few-shot catch cites nothing real, however big the pool."""
        from persona.initiative import CitationKind
        from persona_runtime.initiative.scan import _AliasMap

        aliases = _AliasMap()
        nodes = [aliases.alias(CitationKind.NODE, f"node-{i}") for i in range(1, 1000)]
        tasks = [aliases.alias(CitationKind.TASK, f"task-{i}") for i in range(1, 1000)]
        assert {"N900", "T900"} == EXAMPLE_ALIASES
        assert not EXAMPLE_ALIASES & set(nodes + tasks)
        assert nodes[899] == "N901"  # the 900th node skips the example's code
        assert len(set(nodes)) == len(nodes)

    @pytest.mark.asyncio
    async def test_the_opportunity_key_is_stable_when_the_alias_numbering_changes(self) -> None:
        """Decline memory and the duplicate slot key on REAL ids, whatever the order."""
        first, second = _node("node-91"), _node("node-92", content="The lease ends in March.")
        catch_91_as_n1 = _StubBackend(_CATCH)
        catch_91_as_n2 = _StubBackend(_CATCH.replace('"N1"', '"N2"'))
        one = await _scanner(_Readers(pool=[first, second]), catch_91_as_n1).scan(
            "u", "p", fire_time=_FIRE
        )
        two = await _scanner(_Readers(pool=[second, first]), catch_91_as_n2).scan(
            "u", "p", fire_time=_FIRE
        )
        assert one[0].opportunity_key == two[0].opportunity_key
        assert one[0].opportunity_key == "approaching_commitment:node/node-91"

    @pytest.mark.asyncio
    async def test_the_scan_output_cap_is_the_setting(self) -> None:
        backend = _StubBackend()
        settings = InitiativeSettings(scan_max_output_tokens=5000)
        scanner = InitiativeScanner(
            graph=_Readers(pool=[_node()]),
            tasks=_Readers(),
            backend=backend,
            settings=settings,
        )
        await scanner.scan("u", "p", fire_time=_FIRE)
        assert backend.last_kwargs["max_tokens"] == 5000
        assert InitiativeSettings().scan_max_output_tokens == 6144

    @pytest.mark.asyncio
    async def test_lens_link_targets_are_citable(self) -> None:
        """A node surfaced BY a lens expansion is real retrieval output."""
        from persona_runtime.initiative import ScanLink

        pool = [
            _node(
                links=(
                    ScanLink(
                        link_type="temporal",
                        target_id="node-derived",
                        target_content="The filing precedes the hearing.",
                    ),
                )
            )
        ]
        backend = _StubBackend(_CATCH.replace('"N1"', '"N2"'))
        result = await _scanner(_Readers(pool=pool), backend).scan("u", "p", fire_time=_FIRE)
        assert len(result) == 1
        assert result[0].citations[0].ref == "node-derived"

    @pytest.mark.asyncio
    async def test_free_form_trigger_drops_the_candidate(self) -> None:
        loose = _CATCH.replace("approaching_commitment", "interesting_observation")
        backend = _StubBackend(loose)
        result = await _scanner(_Readers(pool=[_node()]), backend).scan("u", "p", fire_time=_FIRE)
        assert result == ()

    @pytest.mark.asyncio
    async def test_candidate_cap_truncates_rank_ordered(self) -> None:
        import json

        one = json.loads(_CATCH)["candidates"][0]
        flood = json.dumps({"candidates": [one] * 7})
        backend = _StubBackend(flood)
        result = await _scanner(_Readers(pool=[_node()]), backend).scan("u", "p", fire_time=_FIRE)
        # The cap bounds how many are even CONSIDERED (3 by default); identical
        # opportunity keys collapse later at the ledger — here we assert the cap.
        assert len(result) <= _SETTINGS.scan_candidate_cap


class TestSubjectExclusion:
    @pytest.mark.asyncio
    async def test_wellbeing_tagged_node_never_reaches_the_prompt(self) -> None:
        """Criterion 6, scan side: even a misbehaving reader cannot put tagged
        content in front of the scan model (defense-in-depth over the read)."""
        tagged = _node(
            "node-sensitive",
            content="disclosed self-harm struggles last month",
            wellbeing_category="self_harm",
        )
        backend = _StubBackend()
        await _scanner(_Readers(pool=[tagged, _node()]), backend).scan("u", "p", fire_time=_FIRE)
        assert backend.last_user_prompt is not None
        assert "node-sensitive" not in backend.last_user_prompt
        assert "self-harm" not in backend.last_user_prompt
        assert "[N2]" not in backend.last_user_prompt  # the tagged node got no alias either

    @pytest.mark.asyncio
    async def test_self_node_never_reaches_the_prompt(self) -> None:
        backend = _StubBackend()
        await _scanner(_Readers(pool=[_node("node-self", kind="self"), _node()]), backend).scan(
            "u", "p", fire_time=_FIRE
        )
        assert backend.last_user_prompt is not None
        assert "node-self" not in backend.last_user_prompt


class TestFailSoft:
    @pytest.mark.asyncio
    async def test_reader_error_yields_silence(self) -> None:
        backend = _StubBackend(_CATCH)
        result = await _scanner(_Readers(raises=True), backend).scan("u", "p", fire_time=_FIRE)
        assert result == ()
        assert backend.calls == 0

    @pytest.mark.asyncio
    async def test_backend_error_yields_silence(self) -> None:
        backend = _StubBackend(raises=True)
        result = await _scanner(_Readers(pool=[_node()]), backend).scan("u", "p", fire_time=_FIRE)
        assert result == ()

    @pytest.mark.asyncio
    async def test_garbage_output_yields_silence(self) -> None:
        backend = _StubBackend("Here are my thoughts on what you should tell the user...")
        result = await _scanner(_Readers(pool=[_node()]), backend).scan("u", "p", fire_time=_FIRE)
        assert result == ()


class TestPromptArtifact:
    def test_version_constant_and_frozen_few_shots(self) -> None:
        assert INITIATIVE_SCAN_PROMPT_VERSION == "a5-scan-v10"
        # The empty-scan example and every anti-engagement/stale/speculation
        # negative is an EMPTY output — the frozen spec-by-example of restraint.
        assert EXAMPLE_EMPTY_SCAN_OUTPUT == '{"candidates": []}'
        assert EXAMPLE_ENGAGEMENT_DECLINE_OUTPUT == '{"candidates": []}'
        assert EXAMPLE_STALE_DECLINE_OUTPUT == '{"candidates": []}'
        assert EXAMPLE_SPECULATION_DECLINE_OUTPUT == '{"candidates": []}'
        for example in (
            EXAMPLE_EMPTY_SCAN_OUTPUT,
            EXAMPLE_CATCH_OUTPUT,
        ):
            assert example in SCAN_SYSTEM_PROMPT

    def test_prompt_teaches_citing_the_bracketed_code(self) -> None:
        """The few-shot cites never-issued aliases; the rules say to copy the code alone."""
        assert '"ref": "N900"' in EXAMPLE_CATCH_OUTPUT
        assert '"ref": "T900"' in EXAMPLE_CATCH_OUTPUT
        assert "node-91" not in SCAN_SYSTEM_PROMPT
        assert "[N1]" in SCAN_SYSTEM_PROMPT

    def test_prompt_asks_for_the_user_facing_voice(self) -> None:
        """v4 (R9-256): the reason and the offer speak to "you", changeable facts held lightly."""
        import json

        example = json.loads(EXAMPLE_CATCH_OUTPUT)["candidates"][0]
        for field in ("why_now", "next_step"):
            assert "the user" not in example[field].lower()
            assert "you" in example[field].lower().split() or "your" in example[field].lower()
        assert "if the letter isn't drafted yet" in example["why_now"]  # held lightly
        assert 'say "you" and "your", never "the user"' in SCAN_SYSTEM_PROMPT.replace("\n", " ")
        assert "if you haven't compared offers yet" in SCAN_SYSTEM_PROMPT
        assert "never shown to the user" in SCAN_SYSTEM_PROMPT
        # v5: a lowercase verb start (the template keeps the case), and the citation rule's
        # example is a never-issued code.
        assert example["next_step"] == "draft the response letter for you to review"
        assert "starts with a lowercase verb" in " ".join(SCAN_SYSTEM_PROMPT.split())
        assert '(for example "N900")' in SCAN_SYSTEM_PROMPT
        assert '(for example "N2")' not in SCAN_SYSTEM_PROMPT

    def test_prompt_names_no_conversation_material(self) -> None:
        """v3: summaries are not material, so the prompt offers no conversation source."""
        assert "[C1]" not in SCAN_SYSTEM_PROMPT
        assert '"conversation"' not in SCAN_SYSTEM_PROMPT
        assert "conversation summaries" not in SCAN_SYSTEM_PROMPT

    def test_prompt_carries_the_anti_engagement_rule(self) -> None:
        assert "check-ins" in SCAN_SYSTEM_PROMPT or "check-in" in SCAN_SYSTEM_PROMPT
        assert "conversation-starters" in SCAN_SYSTEM_PROMPT

    def test_prompt_carries_the_closed_catalogue(self) -> None:
        for trigger in InitiativeTrigger:
            assert trigger.value in SCAN_SYSTEM_PROMPT


def test_the_v9_prompt_requires_absolute_dates_and_one_advisory_clause() -> None:
    import json

    from persona_runtime.initiative.specifics import relative_day_words

    flat = " ".join(SCAN_SYSTEM_PROMPT.split())
    assert "8. DATES ARE ABSOLUTE." in flat
    assert "its weekday ONLY if the material states one" in flat
    assert '"on 4 October 2026" when it does not' in flat
    assert (
        'Never write "today", "tonight", "tomorrow", "this week", "this weekend", "next week", '
        '"this Friday", "next Tuesday", or a weekday without its date: write the date instead '
        '("by 28 September", not "this weekend"). Never a counted countdown either: "in 3 days", '
        '"3 days left", "two days away", "within a week", "by the weekend", "overnight". After '
        'the date you may add AT MOST ONE short advisory clause, such as "so you may want to '
        'start soon" or "there is still time"; nothing vaguer stands without the date.'
    ) in flat
    # Ruling 2 (2026-10-30 MID set): "this weekend" and "this week" on top of a stated date
    # were refused (A6, C4); the prompt names them, the mechanical rule is unchanged.
    from persona_runtime.initiative.specifics import relative_day_words as _relative

    for word in ("this week", "this weekend", "next week", "tomorrow", "tonight"):
        assert f'"{word}"' in flat
        assert _relative(f"It is due on 28 September 2026, so {word} works.", dated=True)
    example = json.loads(EXAMPLE_CATCH_OUTPUT)["candidates"][0]
    from persona_runtime.initiative.specifics import states_a_date, vague_hedges

    dated = states_a_date(example["why_now"], example["next_step"])
    assert dated  # the example states its date and keeps one advisory clause
    assert relative_day_words(example["why_now"], dated=dated) == []
    assert relative_day_words(example["next_step"], dated=dated) == []
    assert len(vague_hedges(example["why_now"], example["next_step"])) == 1


class TestTheV10Prompt:
    """Rulings after the second MID set: B11 (a speculated date is never grounds, an offer
    to look it up included) and B9 ("tomorrow" next to the date every time)."""

    @staticmethod
    def _flat() -> str:
        return " ".join(SCAN_SYSTEM_PROMPT.split())

    def test_a_speculated_date_is_never_grounds(self) -> None:
        assert (
            'A date the material only guesses at (with words like "might", "perhaps", "I think", '
            '"probably", "maybe", "not sure", "unconfirmed") is never grounds to raise anything, '
            "not even an offer to look it up or confirm it."
        ) in self._flat()

    def test_a_guessed_date_is_a_negative_example_unlike_any_fixture(self) -> None:
        """Review MEDIUM-1: the example shares nothing with the A5-R-1 fixtures, so a
        measuring run tests generalisation, not recall of the example."""
        assert (
            "- The user is not sure when the rowing club's membership dues fall due, perhaps in "
            "spring; offering to find out the date is still raising it → "
            f"{EXAMPLE_SPECULATION_DECLINE_OUTPUT}"
        ) in self._flat()

    def test_the_day_after_today_is_written_as_its_date(self) -> None:
        from persona_runtime.initiative.specifics import relative_day_words

        flat = self._flat()
        sentence = (
            "Worked example: TODAY is day D and the choir concert is on day D+1. Write "
            '"your choir concert on" followed by the weekday and date the material gives, never '
            '"tomorrow" and never "tomorrow" next to the date.'
        )
        assert sentence in flat
        for fixture_term in ("Bergen", "flight", "September 2026", "Christmas", "grant"):
            assert fixture_term not in flat, fixture_term
        assert relative_day_words("your choir concert on Friday 12 March", dated=True) == []
