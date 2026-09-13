"""Tests for the A3 action-category taxonomy + tool→category mapping (A3-D-1, A3-D-X-completeness).

The safety properties under test:

- the eight categories exist and partition cleanly into free / gated-by-default;
- every existing tool maps explicitly (the completeness review — no unmapped tool is
  reachable unattended by accident);
- an unmapped tool / any MCP tool resolves to the most-restrictive **gated** category
  (``unmapped = gated, never free`` — the back-door closure);
- ``code_execution`` is ``compute`` only while the sandbox has **no** network egress, and
  **escalates to ``external_mutate``** once network egress is enabled (the bounded-but-real
  external reach the gate exists to catch).
"""

from __future__ import annotations

import pytest
from persona.tools.catalog import known_tool_names
from persona.tools.categories import (
    _TOOL_CATEGORIES,
    FREE_CATEGORIES,
    GATED_BY_DEFAULT,
    ActionCategory,
    resolve_action_categories,
    unmapped_catalog_tools,
)


class TestTaxonomy:
    def test_eight_categories_exist(self) -> None:
        assert {c.value for c in ActionCategory} == {
            "observe",
            "compute",
            "draft",
            "notify_user",
            "communicate_as_user",
            "spend",
            "external_mutate",
            "credentialed_access",
        }

    def test_free_and_gated_partition_is_clean(self) -> None:
        # Disjoint and exhaustive — every category is exactly one of free / gated-by-default.
        assert FREE_CATEGORIES.isdisjoint(GATED_BY_DEFAULT)
        assert set(ActionCategory) == FREE_CATEGORIES | GATED_BY_DEFAULT

    def test_free_categories_are_the_four_safe_ones(self) -> None:
        assert {
            ActionCategory.OBSERVE,
            ActionCategory.COMPUTE,
            ActionCategory.DRAFT,
            ActionCategory.NOTIFY_USER,
        } == FREE_CATEGORIES

    def test_gated_categories_are_the_four_consequential_ones(self) -> None:
        assert {
            ActionCategory.COMMUNICATE_AS_USER,
            ActionCategory.SPEND,
            ActionCategory.EXTERNAL_MUTATE,
            ActionCategory.CREDENTIALED_ACCESS,
        } == GATED_BY_DEFAULT


class TestResolveBuiltins:
    def test_read_tools_are_observe(self) -> None:
        for name in ("web_search", "web_fetch", "file_read", "datetime", "currency_convert"):
            assert resolve_action_categories(name) == {ActionCategory.OBSERVE}

    def test_transform_tools_are_compute(self) -> None:
        for name in ("calculator", "regex_match", "text_diff", "text_summarize", "json_query"):
            assert resolve_action_categories(name) == {ActionCategory.COMPUTE}

    def test_artifact_tools_are_draft(self) -> None:
        for name in ("file_write", "generate_image", "render_diagram"):
            assert resolve_action_categories(name) == {ActionCategory.DRAFT}

    def test_every_builtin_resolves_to_free_categories_by_default(self) -> None:
        # The whole current built-in surface is low-risk (the industry auto-approve tier):
        # with the default network-off sandbox, nothing maps to a gated category.
        for name in known_tool_names():
            assert resolve_action_categories(name) <= FREE_CATEGORIES

    def test_use_skill_is_free(self) -> None:
        # Activation loads instructions; the skill's constituent tools are independently
        # gated at their own dispatch, so the activation itself is free.
        assert resolve_action_categories("use_skill") <= FREE_CATEGORIES


class TestCodeExecutionEgressEscalation:
    """The safety-critical case: sandbox egress posture backs the categorization."""

    def test_compute_when_network_disabled(self) -> None:
        assert resolve_action_categories("code_execution") == {ActionCategory.COMPUTE}
        assert resolve_action_categories("code_execution", network_enabled=False) == {
            ActionCategory.COMPUTE
        }

    def test_escalates_to_external_mutate_when_network_enabled(self) -> None:
        # Bounded egress is still external reach — an unattended leg that can POST to an
        # allow-listed host is doing external_mutate; it must gate.
        cats = resolve_action_categories("code_execution", network_enabled=True)
        assert cats == {ActionCategory.COMPUTE, ActionCategory.EXTERNAL_MUTATE}
        assert not cats <= FREE_CATEGORIES  # i.e. it now gates

    def test_network_flag_only_escalates_code_execution(self) -> None:
        # A read tool is unaffected by the sandbox network flag.
        assert resolve_action_categories("web_search", network_enabled=True) == {
            ActionCategory.OBSERVE
        }


class TestBackDoorClosure:
    def test_unknown_builtin_defaults_to_gated(self) -> None:
        cats = resolve_action_categories("some_future_unmapped_tool")
        assert cats == {ActionCategory.EXTERNAL_MUTATE}
        assert not cats <= FREE_CATEGORIES

    def test_mcp_tools_default_to_gated(self) -> None:
        for name in ("mcp:deepwiki:read", "mcp:stripe:create_charge", "mcp:unknown:anything"):
            cats = resolve_action_categories(name)
            assert cats == {ActionCategory.EXTERNAL_MUTATE}
            assert not cats <= FREE_CATEGORIES

    def test_n4_adopted_app_tool_resolves_to_a_gated_category(self) -> None:
        # Spec N4 (N4-D-8 rider): a self-adopted app's tool — absent from any explicit mapping —
        # MUST resolve to a GATED category so A3 gates its autonomous invocation (the credentialed
        # third-party tool can't run free unattended). The A3 back-door closure satisfies this for
        # every ``mcp:*`` name; this pins the N4↔A3 contract explicitly for an adopted app.
        for adopted in ("mcp:notion-remote:search", "mcp:linear:create_issue"):
            cats = resolve_action_categories(adopted)
            assert cats & GATED_BY_DEFAULT, f"{adopted} must gate"
            assert not cats <= FREE_CATEGORIES


class TestRegistrationEnforcement:
    def test_every_catalog_tool_is_explicitly_mapped(self) -> None:
        # The completeness review as an executable invariant: a future tool added to the
        # catalog without an explicit category mapping fails this test (it must not be
        # silently carried by the gated default).
        assert unmapped_catalog_tools() == frozenset()


# --- self-knowledge and self-work (Spec W1, D-W1-45) ------------------------


@pytest.mark.parametrize(
    "tool",
    ["task_introspect", "schedule_introspect", "record_user_fact", "task_pickup"],
)
def test_a_leg_may_look_at_and_pick_up_its_own_work_unattended(tool: str) -> None:
    """D-W1-45. These four reach only the owner's own durable state through owner-scoped
    ports, so an unattended leg runs them without an approval.

    Found by the W1 operator pass, on the first real scheduled fire: none of them was in the
    mapping, so each fell to the gated default and the leg parked the task on an approval the
    moment the persona looked at its own tasks. A persona that must ask permission to see its
    own work is the half-feature T9 existed to end.
    """
    assert resolve_action_categories(tool) <= FREE_CATEGORIES


def test_every_tool_the_factory_composes_is_mapped() -> None:
    """The back-door closure cuts both ways: an unmapped tool is gated, which is safe, and
    silently unusable inside a leg, which is not. A tool composed into the toolbox and not
    mapped here is a leg that parks the first time a model reaches for it.
    """
    composed = {
        "web_search",
        "web_fetch",
        "file_read",
        "file_write",
        "code_execution",
        "calculator",
        "datetime",
        "json_query",
        "regex_match",
        "text_diff",
        "text_summarize",
        "currency_convert",
        "generate_image",
        "render_diagram",
        "use_skill",
        "mcp_search",
        "task_introspect",
        "task_pickup",
        "schedule_introspect",
        "record_user_fact",
    }
    unmapped = {tool for tool in composed if tool not in _TOOL_CATEGORIES}
    assert unmapped == set(), f"composed but unmapped (gated + unusable in a leg): {unmapped}"
