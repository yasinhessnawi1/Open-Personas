"""A tool name the model can actually be offered (the MCP colon 400).

Every OpenAI-compatible provider validates function names against ``[a-zA-Z0-9_-]`` and
rejects the WHOLE request when one fails, not just the offending tool. Our MCP tools are
named ``mcp:<server>:<tool>``, so a single one in the toolbox 400s every turn the persona
takes:

    Validation: Function at index 8 has an invalid name: "mcp:calculator:calculate".
    Only a-z, A-Z, 0-9, underscores, and dashes are allowed.

Seen in production on 2026-09-15: every Telegram turn failed with the generic apology while
the persona itself was fine. It reached the model at all only because N7 made an enabled MCP
server expand into its real tools, so the names had somewhere to go.

The translation lives in the Toolbox because that is the one place that already maps a name
to a tool, which means one fix covers every provider and every surface rather than one per
adapter.
"""

from __future__ import annotations

import re

import pytest
from persona.schema.tools import ToolCall, ToolResult
from persona.tools.protocol import tool
from persona.tools.toolbox import Toolbox, wire_tool_name

_WIRE_LEGAL = re.compile(r"[a-zA-Z0-9_-]{1,64}")


def _probe(name: str):  # noqa: ANN202
    @tool(name=name, description=f"probe for {name}")
    async def _t(value: str = "") -> ToolResult:
        return ToolResult(tool_name=name, content=f"{name} ran with {value!r}")

    return _t


class TestWireNameIsIdentityForLegalNames:
    """The property that makes this safe to apply to everything."""

    @pytest.mark.parametrize(
        "name",
        ["calculator", "web_search", "use_skill", "file_read", "generate_image", "a-b_c9"],
    )
    def test_an_already_legal_name_is_untouched(self, name: str) -> None:
        assert wire_tool_name(name) == name

    def test_a_colon_name_becomes_legal(self) -> None:
        assert wire_tool_name("mcp:calculator:calculate") == "mcp_calculator_calculate"

    def test_the_provider_length_cap_is_respected(self) -> None:
        assert len(wire_tool_name("mcp:" + "x" * 200)) <= 64


class TestTheModelIsOnlyOfferedLegalNames:
    def test_every_advertised_spec_would_pass_provider_validation(self) -> None:
        box = Toolbox(
            [_probe("mcp:calculator:calculate"), _probe("web_search")],
            allow_list=["mcp:calculator:calculate", "web_search"],
        )
        for spec in box.get_specs():
            assert _WIRE_LEGAL.fullmatch(spec.name), f"{spec.name!r} would 400 the whole turn"

    def test_the_registered_name_is_unchanged(self) -> None:
        """Only the wire view is renamed; the toolbox still knows its real inventory."""
        box = Toolbox([_probe("mcp:calculator:calculate")], allow_list=None)
        assert box.names() == ["mcp:calculator:calculate"]


class TestTheCallComesBackAndDispatches:
    @pytest.mark.asyncio
    async def test_a_call_by_wire_name_reaches_the_real_tool(self) -> None:
        box = Toolbox([_probe("mcp:calculator:calculate")], allow_list=["mcp:calculator:calculate"])
        result = await box.dispatch(
            ToolCall(name="mcp_calculator_calculate", args={"value": "2+2"}, call_id="c1")
        )
        assert "mcp:calculator:calculate ran" in result.content

    @pytest.mark.asyncio
    async def test_a_call_by_the_real_name_still_works(self) -> None:
        """Nothing that already spoke the real name has to change: voice, replays, tests."""
        box = Toolbox([_probe("mcp:calculator:calculate")], allow_list=["mcp:calculator:calculate"])
        result = await box.dispatch(
            ToolCall(name="mcp:calculator:calculate", args={}, call_id="c1")
        )
        assert result.is_error is False

    def test_the_allow_list_is_honoured_through_the_wire_name(self) -> None:
        """The allow-list holds real names, and the model only ever says wire names. If the
        two are not reconciled the gate reads as "not allowed" and the tool is dead."""
        box = Toolbox(
            [_probe("mcp:calculator:calculate"), _probe("mcp:secrets:read")],
            allow_list=["mcp:calculator:calculate"],
        )
        assert box.is_allowed("mcp_calculator_calculate") is True
        assert box.is_allowed("mcp_secrets_read") is False

    def test_the_mcp_source_badge_survives_the_round_trip(self) -> None:
        """The badge keys off the ``mcp:`` prefix, which wire-safety strips. Without
        resolving first, every MCP call renders as a built-in."""
        box = Toolbox([_probe("mcp:calculator:calculate")], allow_list=None)
        assert box.kind_for("mcp_calculator_calculate") == "mcp:builtin"


class TestCollisionsCannotMisdispatch:
    """Dispatching tool B when the model asked for A is indistinguishable from the model
    misbehaving, so a collision must not be resolved by luck."""

    def test_two_names_that_sanitise_alike_get_distinct_wire_names(self) -> None:
        box = Toolbox([_probe("mcp:a:b"), _probe("mcp_a_b")], allow_list=None)
        wire = [spec.name for spec in box.get_specs()]
        assert len(set(wire)) == 2, f"collision left two tools sharing one name: {wire}"

    def test_the_already_legal_name_keeps_its_own_spelling(self) -> None:
        box = Toolbox([_probe("mcp:a:b"), _probe("mcp_a_b")], allow_list=None)
        assert box.real_name("mcp_a_b") == "mcp_a_b"

    @pytest.mark.asyncio
    async def test_each_collided_tool_still_reaches_itself(self) -> None:
        box = Toolbox([_probe("mcp:a:b"), _probe("mcp_a_b")], allow_list=None)
        for spec in box.get_specs():
            result = await box.dispatch(ToolCall(name=spec.name, args={}, call_id="c"))
            assert box.real_name(spec.name) in result.content

    def test_the_mapping_is_stable_across_processes(self) -> None:
        """A conversation's history outlives a redeploy, so the wire name a model was shown
        yesterday must still resolve today. Hash-based, not enumeration-based."""
        first = Toolbox([_probe("mcp:a:b"), _probe("mcp_a_b")], allow_list=None)
        second = Toolbox([_probe("mcp_a_b"), _probe("mcp:a:b")], allow_list=None)
        assert sorted(s.name for s in first.get_specs()) == sorted(
            s.name for s in second.get_specs()
        )
