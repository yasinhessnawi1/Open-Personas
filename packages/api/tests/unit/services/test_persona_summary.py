"""Unit tests for ``persona_service.summary_of`` — Spec 35 capability glance.

The library card's capability/identity glance (language + apps&tools + skills +
constraints) is parsed from the SAME stored YAML the list query already loads —
no extra query. ``tools_count`` folds MCP servers (a persona enables a server by
carrying ``mcp:<name>`` in its ``tools`` list), so it reads as one "apps & tools"
count. ``conversation_count`` is supplied by the caller (one GROUP-BY per page).
"""

from __future__ import annotations

import datetime as dt

from persona_api.services.persona_service import summary_of

_NOW = dt.datetime(2026, 6, 19, tzinfo=dt.UTC)


def _row(yaml_str: str) -> dict[str, object]:
    return {
        "id": "p1",
        "yaml": yaml_str,
        "avatar_url": None,
        "created_at": _NOW,
        "updated_at": _NOW,
    }


def test_parses_language_and_counts_folding_mcp() -> None:
    yaml_str = """
identity:
  name: Astrid
  role: Tenancy law assistant
  language_default: no
  constraints:
    - never gives binding legal advice
    - cites the tenancy act
tools:
  - web_search
  - mcp:husleie:search_cases
skills:
  - drafting
"""
    s = summary_of(_row(yaml_str), conversation_count=4)
    assert s.name == "Astrid"
    assert s.role == "Tenancy law assistant"
    assert s.language == "no"
    # built-in tool + mcp: entry both counted (folded "apps & tools").
    assert s.tools_count == 2
    assert s.skills_count == 1
    assert s.constraints_count == 2
    assert s.conversation_count == 4


def test_defaults_on_minimal_yaml() -> None:
    s = summary_of(_row("identity:\n  name: Bare\n  role: r\n"))
    assert s.language == "en"
    assert s.tools_count == 0
    assert s.skills_count == 0
    assert s.constraints_count == 0
    assert s.conversation_count == 0


def test_survives_malformed_yaml() -> None:
    # A malformed stored YAML still lists, just without the extras.
    s = summary_of(_row("identity: [unterminated"))
    assert s.tools_count == 0
    assert s.constraints_count == 0
    assert s.conversation_count == 0


# ----- R9-155: the one bit the list view cannot parse for itself ------------


def test_form_is_surfaced_for_a_synthetic_persona() -> None:
    """The library card has no YAML to read, so the bit it needs travels as a field."""
    yaml_str = """
identity:
  name: JARVIS
  role: Personal chief of staff
  presentation:
    form: synthetic
    presents: masculine
"""
    assert summary_of(_row(yaml_str)).form == "synthetic"


def test_form_is_surfaced_for_a_human_persona() -> None:
    yaml_str = """
identity:
  name: Astrid
  role: Tenancy law assistant
  presentation:
    form: human
    presents: feminine
"""
    assert summary_of(_row(yaml_str)).form == "human"


def test_a_persona_that_never_declared_reports_no_form() -> None:
    """Not a default of human: the card renders it exactly as it does today."""
    yaml_str = """
identity:
  name: Astrid
  role: Tenancy law assistant
"""
    assert summary_of(_row(yaml_str)).form is None


def test_a_malformed_presentation_does_not_break_the_card() -> None:
    """A list row is not the place to fail on a document having a bad day."""
    for broken in ("presentation: []", "presentation:\n    form: robot", "presentation: null"):
        yaml_str = f"identity:\n  name: A\n  role: B\n  {broken}\n"
        assert summary_of(_row(yaml_str)).form is None
