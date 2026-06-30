"""B1 vetted-source-authenticity checkpoint — the Anthropic adapter (Spec S2).

This is a **security checkpoint inside batch B**, tested like Group A's contract,
not like scaffolding (the C4-T3 pattern). ``vetted`` is the only tier that
bypasses consent (``SkillTrust.requires_consent`` is ``False``), so the integrity
of "vetted" rests entirely on the adapter proving the source is *genuinely* the
pinned canonical Anthropic coordinate. Two distinct decisions are proven here:

- **S2-D-4** — swap the repo URL or the commit ⇒ **refuse to stamp ``vetted``**
  (raise ``VettedSourceAuthenticityError`` — a sync error, never a silent
  downgrade to a consent-gated tier: fail-closed, not fail-quiet).
- **S2-D-5** — the pin is a **code constant the auto-sync cannot advance**: a
  re-fetch of the pinned content is idempotent, and there is no path by which a
  sync could bump the SHA to a future (possibly compromised) commit at ``vetted``.
"""

from __future__ import annotations

from pathlib import Path  # noqa: TC003 — used in fixture helpers at runtime

import pytest
from persona.errors import VettedSourceAuthenticityError
from persona.schema.skills import SkillTrust
from persona.skills.sources import anthropic
from persona.skills.sources.anthropic import (
    ANTHROPIC_PINNED_COMMIT,
    ANTHROPIC_REPO_URL,
    ANTHROPIC_SOURCE_ID,
    ingest_anthropic_skills,
    verify_vetted_coordinate,
)

_VALID_SKILL = """\
---
name: pdf_helper
description: Extract text and fill forms in PDFs. Use when working with PDFs.
---
PDF instructions.
"""


def _make_checkout(base: Path, *names: str) -> Path:
    """Build a fake Anthropic checkout: ``base/skills/<name>/SKILL.md`` for each name."""
    root = base / "checkout"
    for name in names:
        skill_dir = root / "skills" / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(_VALID_SKILL.replace("pdf_helper", name), "utf-8")
    return root


# --- S2-D-4: swap-URI / swap-ref ⇒ refuse to stamp vetted -------------------


def test_swapped_repo_url_refuses_to_stamp_vetted() -> None:
    """A non-canonical repo URL is refused — vetting a name, not an endpoint, would
    let an attacker get 'anthropic' pointed at their own repo at the bypass tier.
    """
    with pytest.raises(VettedSourceAuthenticityError):
        verify_vetted_coordinate(
            fetched_repo_url="https://github.com/attacker/skills",
            fetched_commit=ANTHROPIC_PINNED_COMMIT,
        )


def test_wrong_commit_refuses_to_stamp_vetted() -> None:
    """A checkout at any commit other than the pinned one is refused."""
    with pytest.raises(VettedSourceAuthenticityError):
        verify_vetted_coordinate(
            fetched_repo_url=ANTHROPIC_REPO_URL,
            fetched_commit="0000000000000000000000000000000000000000",
        )


def test_refusal_is_an_error_not_a_silent_downgrade(tmp_path: Path) -> None:
    """The whole ingest refuses on mismatch — it does NOT return a downgraded,
    consent-gated spec (fail-closed, not fail-quiet — the S2-D-4 lock).
    """
    checkout = _make_checkout(tmp_path, "pdf_helper")
    with pytest.raises(VettedSourceAuthenticityError):
        ingest_anthropic_skills(
            checkout,
            fetched_repo_url="https://github.com/attacker/skills",
            fetched_commit=ANTHROPIC_PINNED_COMMIT,
        )


# --- S2-D-5: the pin is a code constant the auto-sync cannot advance ---------


def test_pinned_commit_is_a_module_constant() -> None:
    """The pin lives in code (a module constant), the vetting record itself —
    advancing it is a reviewed code change, not a config/sync action.
    """
    assert isinstance(ANTHROPIC_PINNED_COMMIT, str)
    assert ANTHROPIC_PINNED_COMMIT  # non-empty
    # It is a module-level attribute of the adapter, not a parameter/env read.
    assert anthropic.ANTHROPIC_PINNED_COMMIT == ANTHROPIC_PINNED_COMMIT


def test_sync_refetch_of_pinned_content_is_idempotent_and_stamps_vetted(tmp_path: Path) -> None:
    """A sync re-fetching the pinned content (HEAD == pinned constant) verifies and
    stamps ``vetted`` at the pinned ref — the safe, idempotent steady state.
    """
    # Use an allowlist override so this test exercises verify+ingest independent of the
    # real curation (the curated allowlist is covered by the E1 tests below).
    checkout = _make_checkout(tmp_path, "pdf_helper")
    specs = ingest_anthropic_skills(
        checkout,
        fetched_repo_url=ANTHROPIC_REPO_URL,
        fetched_commit=ANTHROPIC_PINNED_COMMIT,
        allowlist=frozenset({"pdf_helper"}),
    )
    assert [s.name for s in specs] == ["pdf_helper"]
    spec = specs[0]
    assert spec.trust is SkillTrust.VETTED
    assert spec.provenance is not None
    assert spec.provenance.source == ANTHROPIC_SOURCE_ID
    # Provenance is stamped with the pinned CONSTANT, never an argument-supplied ref.
    assert spec.provenance.source_ref == ANTHROPIC_PINNED_COMMIT


def test_sync_cannot_advance_pin_to_a_future_commit(tmp_path: Path) -> None:
    """The property that keeps 'vetted' from silently drifting: if upstream moved
    and the sync fetched a *future* commit, verification compares against the
    code-constant pin and REFUSES — the sync can never make a future (possibly
    compromised) commit ride at the consent-bypass tier. Only editing the constant
    (a reviewed commit) can accept new content.
    """
    checkout = _make_checkout(tmp_path, "pdf_helper")
    future_commit = "ffffffffffffffffffffffffffffffffffffffff"
    assert future_commit != ANTHROPIC_PINNED_COMMIT
    with pytest.raises(VettedSourceAuthenticityError):
        ingest_anthropic_skills(
            checkout,
            fetched_repo_url=ANTHROPIC_REPO_URL,
            fetched_commit=future_commit,
        )


# --- E1: the curated vetted allowlist (S2-D-9) ------------------------------


def test_only_allowlisted_skills_ingest_at_vetted(tmp_path: Path) -> None:
    """S2-D-9 default-deny curation: a skill NOT on the vetted allowlist is excluded, even
    from the canonical pinned coordinate — vetted is a default-deny set, not 'whatever the
    repo ships' (the consent-bypass tier earns the strictest curation).
    """
    from persona.skills.sources.anthropic import ANTHROPIC_VETTED_SKILLS

    # One allowlisted, one not — both present in the (verified) checkout.
    allowed_name = next(iter(ANTHROPIC_VETTED_SKILLS))
    checkout = _make_checkout(tmp_path, allowed_name, "not_curated_skill")

    specs = ingest_anthropic_skills(
        checkout,
        fetched_repo_url=ANTHROPIC_REPO_URL,
        fetched_commit=ANTHROPIC_PINNED_COMMIT,
    )

    names = {s.name for s in specs}
    assert allowed_name in names
    assert "not_curated_skill" not in names


def test_vetted_allowlist_excludes_script_dependent_and_builtin_dupes() -> None:
    """The curated set is instructional-complete: no script-dependent skills (they'd degrade
    to text-only under D2) and none duplicating our four builtins.
    """
    from persona.skills.sources.anthropic import ANTHROPIC_VETTED_SKILLS

    # Script-dependent Anthropic skills are excluded (would ship as degraded text-only).
    for script_dep in ("algorithmic-art", "docx", "pdf", "pptx", "mcp-builder", "skill-creator"):
        assert script_dep not in ANTHROPIC_VETTED_SKILLS
    # No duplication of our builtins.
    for builtin in ("web_research", "data_analysis", "document_generation", "code_review"):
        assert builtin not in ANTHROPIC_VETTED_SKILLS
    assert len(ANTHROPIC_VETTED_SKILLS) > 0  # the library is non-empty


def test_pinned_commit_is_a_real_40_hex_sha() -> None:
    """E1 = the vetting act: the placeholder is replaced by a real reviewed commit SHA."""
    assert len(ANTHROPIC_PINNED_COMMIT) == 40
    assert all(c in "0123456789abcdef" for c in ANTHROPIC_PINNED_COMMIT)
