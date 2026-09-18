"""Skills layer — scanner, injector, index renderer, and the synthetic
``use_skill`` activation tool (spec 04).

Public surface:

- :class:`persona.schema.skills.SkillSpec` — the Pydantic model (extended
  additively in spec 04 per D-04-1; spec-01's four-field shape preserved).
- :class:`SkillScanner` — discovers declared skills under one or more
  paths, parses YAML front matter, validates ``tools_required`` against
  the persona's allow-list.
- :func:`render_skill_index` — pure function producing the compact
  "available skills" Markdown block injected into every system prompt.
- :class:`SkillInjector` — enforces the 2000-token-per-turn skill content
  budget; verbatim / cached-summary / truncate branches.
- :class:`SkillSummaryCache` + :func:`ensure_skill_summaries` — the
  once-and-cached summariser behind the middle branch (R9-165): summaries
  are made at boot or at the mirror sync, keyed on the body's content
  hash, and never inside ``inject``.
- :func:`make_use_skill_tool` — factory producing the synthetic
  ``use_skill`` :class:`persona.tools.AsyncTool` (Pattern-1 activation per
  D-04-9). Spec 05's runtime composes this into the toolbox when the
  persona declares skills (D-04-10).
- :func:`count_tokens` — wraps ``tiktoken cl100k_base`` for any downstream
  consumer that needs the same encoder.
- :exc:`SkillManifestError` — raised by the front-matter parser; caught
  by the scanner's per-skill envelope (D-04-4).
"""

from __future__ import annotations

from pathlib import Path

from persona.errors import SkillManifestError
from persona.schema.skills import SkillSpec
from persona.skills._tokens import count_tokens
from persona.skills.consent import (
    DenyUnvettedConsent,
    SkillConsentPort,
    injection_consent_state,
    skill_audit_event,
)
from persona.skills.guard import (
    DEFENSE_CLAIM,
    SUBORDINATION_PREAMBLE,
    default_nonce,
    self_framed,
    subordinate,
)
from persona.skills.index import render_skill_index
from persona.skills.injector import SkillInjector, SkillSummaryLookup
from persona.skills.scanner import SkillScanner
from persona.skills.summary import (
    SkillSummaryCache,
    SkillSummaryReport,
    ensure_skill_summaries,
    resolve_skill_summary_cache_path,
)
from persona.skills.use_skill_tool import collect_skill_supplements, make_use_skill_tool

#: Filesystem path to the bundled built-in skills directory. Shared by
#: persona-api services that wire :class:`SkillScanner` so the catalog
#: endpoint and the runtime composition root stay in lockstep; the
#: directory ships as package data via persona-core's ``pyproject.toml``
#: ``[tool.hatch.build.targets.wheel].include`` entries
#: (``src/persona/skills/builtin/**/SKILL.md`` + ``supplements/*.md``).
BUILTIN_ROOT: Path = Path(__file__).parent / "builtin"

__all__ = [
    "BUILTIN_ROOT",
    "DEFENSE_CLAIM",
    "SUBORDINATION_PREAMBLE",
    "DenyUnvettedConsent",
    "SkillConsentPort",
    "SkillInjector",
    "SkillManifestError",
    "SkillScanner",
    "SkillSpec",
    "SkillSummaryCache",
    "SkillSummaryLookup",
    "SkillSummaryReport",
    "collect_skill_supplements",
    "count_tokens",
    "default_nonce",
    "ensure_skill_summaries",
    "injection_consent_state",
    "make_use_skill_tool",
    "render_skill_index",
    "resolve_skill_summary_cache_path",
    "self_framed",
    "skill_audit_event",
    "subordinate",
]
