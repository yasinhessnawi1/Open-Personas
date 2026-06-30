"""Native external-skill ingest — source-assigned trust + provenance (Spec S2, A1).

``ingest_external_skill`` is the thin wrapper over the scanner's parse path
(S2-D-2): it reuses :func:`persona.skills._frontmatter.parse_skill_markdown` +
``SkillSpec`` validation + the same warn-and-skip envelope (D-04-4), then
**overrides** ``trust`` + ``SkillProvenance`` with the values assigned by the
*source* and recomputes ``content_hash = sha256(body)``.

**The load-bearing rule (S2-D-3 / S1-D-3):** trust + provenance are
**source-assigned, never self-declared**. The front matter is author-controlled,
so a ``trust:`` / ``source:`` / ``provenance:`` key in it is **deliberately
ignored** — a hostile skill declaring ``trust: builtin`` must still ride at the
source-assigned tier, or the whole tier model (and the consent gate behind it)
collapses. Likewise ``allowed-tools`` is not honored as a grant (S2-D-10) — the
persona's own allow-list governs tool access; this ingest does not read it.

The scanner's builtin path (``SkillScanner._scan_one``) is left **untouched**;
this is a separate entry point for the external sources, so the builtin ingest
stays byte-identical and auditable.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from pydantic import ValidationError

from persona.errors import SkillManifestError
from persona.logging import get_logger
from persona.schema.skills import SkillProvenance, SkillSpec, SkillTrust
from persona.skills._frontmatter import parse_skill_markdown
from persona.skills._tokens import count_tokens

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

__all__ = ["ingest_external_skill", "ingest_external_skills"]

_logger = get_logger("skills.sources.ingest")


def ingest_external_skill(
    skill_md: Path,
    *,
    trust: SkillTrust,
    source: str,
    source_uri: str | None = None,
    source_ref: str | None = None,
) -> SkillSpec | None:
    """Ingest one external ``SKILL.md``, assigning trust + provenance at the source.

    Reuses the scanner's parse + validation; the ``trust`` and ``provenance``
    are **source-assigned** (the caller — a per-source adapter — passes the tier
    it owns), never read from the front matter (S2-D-3). ``content_hash`` is the
    real sha256 of the parsed body, the re-consent handle (S1-D-5).

    Args:
        skill_md: Path to the external skill's ``SKILL.md`` file.
        trust: The source-assigned trust tier (Anthropic=``vetted``,
            OpenClaw=``community``, arbitrary GitHub=``third_party``).
        source: The source id recorded in provenance (e.g. ``"anthropic"``,
            ``"openclaw"``, ``"github:owner/repo"``).
        source_uri: The source URL where applicable (the repo/catalog URL).
        source_ref: The source ref where available (a pinned commit SHA /
            catalog version).

    Returns:
        The ingested ``SkillSpec`` with source-assigned trust + provenance, or
        ``None`` on any per-skill failure (malformed front matter, validation
        error, unreadable file) — warn-and-skip, mirroring the scanner so one
        bad external skill never aborts a sync (D-04-4 / criterion 1).
    """
    name = skill_md.parent.name
    try:
        meta, body = parse_skill_markdown(skill_md)
        # Spec 24 v2 fields live under the ``metadata`` escape hatch; a non-mapping
        # ``metadata`` yields no v2 fields rather than dropping the skill.
        md = meta.get("metadata")
        if not isinstance(md, dict):
            md = {}
        spec = SkillSpec(
            name=meta.get("name", name),
            description=meta["description"],
            path=skill_md.parent,
            when_to_use=meta.get("when_to_use"),
            tools_required=list(meta.get("tools_required") or []),
            content=body,
            content_token_count=count_tokens(body),
            parameters=md.get("parameters"),
            not_for=list(md.get("not_for") or []),
            composes_with=list(md.get("composes_with") or []),
            output_format=md.get("output_format"),
            token_budget=md.get("token_budget"),
            # S2-D-3: trust + provenance are SOURCE-assigned. We DELIBERATELY do
            # not read ``trust`` / ``source`` / ``provenance`` from ``meta`` /
            # ``md`` (author-controlled front matter) — a hostile skill cannot
            # self-declare a higher trust. ``content_hash`` is the real sha256 of
            # the parsed body (S1-D-5), never a front-matter-supplied value.
            trust=trust,
            provenance=SkillProvenance(
                source=source,
                source_uri=source_uri,
                source_ref=source_ref,
                content_hash=hashlib.sha256(body.encode("utf-8")).hexdigest(),
            ),
        )
    except SkillManifestError as e:
        _logger.warning(
            "external skill manifest invalid",
            skill=name,
            path=str(skill_md),
            source=source,
            reason=str(e),
        )
        return None
    except ValidationError as e:
        _logger.warning(
            "external skill spec validation failed",
            skill=name,
            path=str(skill_md),
            source=source,
            errors=[err.get("msg", "") for err in e.errors()],
        )
        return None
    except KeyError as e:
        # meta["description"] missing — required by SkillSpec.
        _logger.warning(
            "external skill front matter missing required field",
            skill=name,
            path=str(skill_md),
            source=source,
            field=str(e),
        )
        return None
    except Exception as e:  # noqa: BLE001 — D-04-4 broad warn-and-skip envelope
        _logger.warning(
            "external skill ingest failed",
            skill=name,
            path=str(skill_md),
            source=source,
            exc_type=type(e).__name__,
            exc=str(e)[:200],
        )
        return None

    return spec


def ingest_external_skills(
    skill_mds: Iterable[Path],
    *,
    trust: SkillTrust,
    source: str,
    source_uri: str | None = None,
    source_ref: str | None = None,
) -> list[SkillSpec]:
    """Ingest many external ``SKILL.md`` files from one source — resilient to drift.

    One source = one repo at one pinned ref, so every skill in the batch shares
    the ``source`` / ``source_uri`` / ``source_ref`` coordinate; each survivor
    still gets its **own** ``SkillProvenance`` (with its own ``content_hash``).
    A per-skill failure is **skipped (warn-and-skip), never fatal to the batch**
    (criterion 1 / the source-format-drift resilience): an upstream layout change
    degrades to "the skills that still parse", never "the whole source goes dark".
    Each skip logs *why* (in :func:`ingest_external_skill`) so a curator can
    diagnose a drifted source — silent-skip and warn-and-skip look identical in a
    test but diverge badly in production.

    Args:
        skill_mds: The discovered ``SKILL.md`` paths for this source.
        trust: The source-assigned trust tier (applied to every skill).
        source: The source id recorded in each skill's provenance.
        source_uri: The source URL recorded in each skill's provenance.
        source_ref: The source ref (pinned commit / catalog version) recorded in
            each skill's provenance.

    Returns:
        One ``SkillSpec`` per skill that ingested cleanly, in input order; failed
        skills are omitted (warned, not raised).
    """
    out: list[SkillSpec] = []
    for skill_md in skill_mds:
        spec = ingest_external_skill(
            skill_md,
            trust=trust,
            source=source,
            source_uri=source_uri,
            source_ref=source_ref,
        )
        if spec is not None:
            out.append(spec)
    return out
