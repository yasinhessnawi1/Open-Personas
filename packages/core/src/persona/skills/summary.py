"""Skill summaries: made once, cached by content hash, never on the turn path (R9-165).

The architecture promises an over-budget skill is *summarised* rather than cut. Until this
module, the injector had a ``summariser`` hook nobody wired, so every over-budget skill was
character-truncated: the built-in ``web_research`` lost 1,070 of its 3,069 tokens on every
injection (its whole source-evaluation rubric), and an ingested skill measured 17,627 tokens
against a 2,000 budget.

The owner's ruling fixes the shape, not just the gap. A skill is static content, so it is
summarised **once** and the result is **cached keyed on the body's content hash**. That gives:

- no model latency on any turn (the injector only does a dictionary lookup);
- the same skill injects the same body every time (a per-turn summary would differ between
  turns, which is worse than truncation);
- invalidation by construction: a changed body has a different hash and simply misses.

Summaries are produced at boot (built-ins + the already-synced mirror, see
``RuntimeFactory.warm_skill_summaries``) and after every skill-mirror sync (the worker's
``SkillCatalogSyncTask.summarise_synced``). When no summariser backend is available, the
cache stays empty and the injector falls back to today's truncation, with its WARNING.

The cache is a small JSON file (``{"version": 1, "summaries": {<hash>: <record>}}``),
written atomically like the skill mirror. Reads re-check the file's mtime so two instances
in one process (the factory's and the sync task's) see each other's writes without any
shared state.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from persona.backends.errors import ProviderError
from persona.logging import get_logger
from persona.schema.conversation import ConversationMessage
from persona.skills._tokens import count_tokens
from persona.skills.injector import _MARKER_TOKENS, content_hash_of, skill_budget

if TYPE_CHECKING:
    from collections.abc import Iterable

    from persona.backends import ChatBackend
    from persona.schema.skills import SkillSpec

__all__ = [
    "SKILL_SUMMARY_CACHE_FILENAME",
    "SKILL_SUMMARY_PROMPT",
    "SkillSummary",
    "SkillSummaryCache",
    "SkillSummaryReport",
    "ensure_skill_summaries",
    "resolve_skill_summary_cache_path",
    "summarise_skill",
]

_log = get_logger("skills.summary")

#: The cache file's name; placed beside the skill mirror (volume) or under ``chroma_path``.
SKILL_SUMMARY_CACHE_FILENAME = "skill_summaries.json"

#: The summariser prompt. Procedure and rubrics are what a skill is for, so they are kept
#: verbatim and prose goes first; headings stay so the model can still navigate the skill.
SKILL_SUMMARY_PROMPT = (
    "You are condensing an agent skill file so it fits a token budget. Rewrite it in at most "
    "{words} words. Keep every heading, in order. Keep every step of every procedure, every "
    "rule, checklist and rubric, and every tool name or command, as written. Cut explanation, "
    "examples and repetition first. Keep the Markdown. Output only the condensed skill, with "
    "no preamble."
)

# English runs about 1.3 cl100k tokens per word; asking for 0.7 words per target token leaves
# room for a small model that runs long. A second attempt asks for two thirds of that.
_WORDS_PER_TOKEN = 0.7
_RETRY_SHRINK = 2 / 3
# Response headroom above the budget so a slightly-long answer is measured, not cut mid-word.
_RESPONSE_HEADROOM_TOKENS = 512
# A skill larger than this is summarised from its head only (bounds one call's cost).
_MAX_INPUT_CHARS = 120_000


class SkillSummary(BaseModel):
    """One cached summary, bound to the exact body it was made from.

    Attributes:
        content_hash: sha256 of the skill body the summary condenses (the cache key).
        summary: The condensed skill body, within ``budget`` tokens.
        summary_token_count: ``cl100k_base`` tokens in ``summary``.
        budget: The budget the summary was made to fit.
        model: The ``provider/model`` that produced it (observability only).
        created_at: When it was produced (tz-aware UTC).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    content_hash: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    summary_token_count: int = Field(ge=1)
    budget: int = Field(ge=1)
    model: str = Field(min_length=1)
    created_at: datetime


class SkillSummaryReport(BaseModel):
    """What one :func:`ensure_skill_summaries` pass did, for the boot/sync logs.

    Attributes:
        summarised: Skills summarised and cached in this pass.
        cached: Over-budget skills that already had a summary for their current body.
        unavailable: Over-budget skills left to truncation because no backend was given.
        failed: Over-budget skills whose summary attempt failed or came back over budget.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    summarised: tuple[str, ...] = ()
    cached: tuple[str, ...] = ()
    unavailable: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()


class SkillSummaryCache:
    """A content-hash keyed summary store: a JSON file, or memory only when ``path`` is None.

    Satisfies the injector's ``SkillSummaryLookup`` shape. Loading is fail-soft (an absent or
    corrupt file is an empty cache); writes are atomic (temp file + rename). ``get`` re-reads
    the file when its mtime changed so another writer's summaries become visible.

    Args:
        path: The cache file. ``None`` keeps the cache in memory for this process only.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._records: dict[str, SkillSummary] = {}
        self._loaded_mtime_ns: int | None = None
        self._reload_if_changed()

    @property
    def path(self) -> Path | None:
        """Where the cache persists, or ``None`` for memory only."""
        return self._path

    def __len__(self) -> int:
        return len(self._records)

    def get(self, content_hash: str, *, budget: int) -> str | None:
        """The cached summary for ``content_hash`` if it fits ``budget``, else ``None``."""
        self._reload_if_changed()
        record = self._records.get(content_hash)
        if record is None or record.summary_token_count > budget:
            return None
        return record.summary

    def put(self, record: SkillSummary) -> None:
        """Store ``record`` under its hash and persist the cache (atomic when file-backed)."""
        self._reload_if_changed()
        self._records[record.content_hash] = record
        self._write()

    # -- persistence -------------------------------------------------------------

    def _reload_if_changed(self) -> None:
        if self._path is None:
            return
        try:
            mtime_ns = self._path.stat().st_mtime_ns
        except OSError:
            # Absent (or unreadable) file: nothing cached on disk yet.
            self._records = {} if self._loaded_mtime_ns is not None else self._records
            self._loaded_mtime_ns = None
            return
        if mtime_ns == self._loaded_mtime_ns:
            return
        self._records = self._read(self._path)
        self._loaded_mtime_ns = mtime_ns

    @staticmethod
    def _read(path: Path) -> dict[str, SkillSummary]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return {
                str(key): SkillSummary.model_validate(item)
                for key, item in dict(raw["summaries"]).items()
            }
        except (OSError, ValueError, KeyError, TypeError, ValidationError) as exc:
            _log.warning(
                "skill summary cache unreadable; starting empty",
                path=str(path),
                error=type(exc).__name__,
            )
            return {}

    def _write(self) -> None:
        if self._path is None:
            return
        payload = {
            "version": 1,
            "summaries": {k: v.model_dump(mode="json") for k, v in self._records.items()},
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self._path.parent), prefix=".skill_summaries.", suffix=".tmp"
        )
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=True)
            tmp.replace(self._path)  # atomic on the same filesystem
        except BaseException:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise
        with contextlib.suppress(OSError):
            self._loaded_mtime_ns = self._path.stat().st_mtime_ns


def resolve_skill_summary_cache_path(
    override: Path | None, *, mirror_path: Path | None, data_root: Path
) -> Path:
    """Where the summary cache lives: the override, else beside the mirror, else ``data_root``.

    A built-in skill is package data (root-owned on a deployed image, and writing there
    dirties a checkout), so "beside the skill" means the same writable place the skill
    mirror already uses: ``PERSONA_SKILL_MIRROR_PATH``'s directory (the mounted volume in
    the hosted deploy). Without a mirror path (dev / community) the cache lands under the
    persistence root (``chroma_path``), next to the audit log.
    """
    if override is not None:
        return override
    if mirror_path is not None:
        return mirror_path.parent / SKILL_SUMMARY_CACHE_FILENAME
    return data_root / SKILL_SUMMARY_CACHE_FILENAME


async def summarise_skill(spec: SkillSpec, backend: ChatBackend, *, budget: int) -> str | None:
    """Condense ``spec.content`` to fit ``budget`` with one small-tier call (one retry).

    Targets the budget minus the truncation marker, the same headroom truncation keeps.
    Returns ``None`` when both attempts came back empty or over budget, so the caller can
    leave the skill to the truncation fallback rather than cache a summary that would be cut.

    Raises:
        ProviderError: the backend failed (the caller decides how loud to be).
    """
    target = max(1, budget - _MARKER_TOKENS)
    body = spec.content[:_MAX_INPUT_CHARS]
    if len(spec.content) > _MAX_INPUT_CHARS:
        _log.warning(
            "skill body is very large; summarising its head only",
            skill=spec.name,
            chars=len(spec.content),
            kept=_MAX_INPUT_CHARS,
        )
    words = max(50, int(target * _WORDS_PER_TOKEN))
    for attempt, word_cap in enumerate((words, int(words * _RETRY_SHRINK)), start=1):
        now = datetime.now(UTC)
        messages = [
            ConversationMessage(
                role="system", content=SKILL_SUMMARY_PROMPT.format(words=word_cap), created_at=now
            ),
            ConversationMessage(role="user", content=body, created_at=now),
        ]
        response = await backend.chat(
            messages, temperature=0.0, max_tokens=budget + _RESPONSE_HEADROOM_TOKENS
        )
        summary = response.content.strip()
        if summary and count_tokens(summary) <= budget:
            return summary
        _log.info(
            "skill summary attempt did not fit the budget",
            skill=spec.name,
            attempt=attempt,
            summary_tokens=count_tokens(summary),
            budget=budget,
        )
    return None


async def ensure_skill_summaries(
    specs: Iterable[SkillSpec],
    *,
    cache: SkillSummaryCache,
    backend: ChatBackend | None,
) -> SkillSummaryReport:
    """Make sure every over-budget skill in ``specs`` has a cached summary for its body.

    Idempotent and cheap on a warm cache: a skill whose current body already has a summary
    costs a dictionary lookup, so this runs at every boot and after every sync without a
    model call. With ``backend=None`` (no small tier configured) nothing is produced; each
    over-budget skill is named at WARNING with its numbers so it is caught at authoring, and
    injection falls back to truncation as before.

    Args:
        specs: The skills that can be injected (built-ins after fidelity resolution, plus the
            mirror's external skills). Under-budget skills are skipped untouched.
        cache: The content-hash keyed store the injector reads.
        backend: The small-tier backend, or ``None`` when unavailable.

    Returns:
        A :class:`SkillSummaryReport` naming what happened per over-budget skill.
    """
    summarised: list[str] = []
    cached: list[str] = []
    unavailable: list[str] = []
    failed: list[str] = []
    for spec in specs:
        budget = skill_budget(spec)
        if spec.content_token_count <= budget:
            continue
        content_hash = content_hash_of(spec)
        if cache.get(content_hash, budget=budget) is not None:
            cached.append(spec.name)
            continue
        over = spec.content_token_count - budget
        if backend is None:
            _log.warning(
                "skill {skill} is {over} tokens over its budget and no summariser is configured "
                "(tokens={tokens}, budget={budget}); it will be truncated at injection. "
                "Shorten the skill, raise its token_budget, or configure a small-tier model.",
                skill=spec.name,
                over=over,
                tokens=spec.content_token_count,
                budget=budget,
            )
            unavailable.append(spec.name)
            continue
        try:
            summary = await summarise_skill(spec, backend, budget=budget)
        except ProviderError as exc:
            _log.warning(
                "skill {skill} summary failed ({error}); it will be truncated at injection "
                "(tokens={tokens}, budget={budget})",
                skill=spec.name,
                error=type(exc).__name__,
                tokens=spec.content_token_count,
                budget=budget,
            )
            failed.append(spec.name)
            continue
        if summary is None:
            _log.warning(
                "skill {skill} summary did not fit the budget after two attempts "
                "(tokens={tokens}, budget={budget}); it will be truncated at injection",
                skill=spec.name,
                tokens=spec.content_token_count,
                budget=budget,
            )
            failed.append(spec.name)
            continue
        cache.put(
            SkillSummary(
                content_hash=content_hash,
                summary=summary,
                summary_token_count=count_tokens(summary),
                budget=budget,
                model=f"{backend.provider_name}/{backend.model_name}",
                created_at=datetime.now(UTC),
            )
        )
        _log.info(
            "skill summarised and cached",
            skill=spec.name,
            tokens=spec.content_token_count,
            summary_tokens=count_tokens(summary),
            budget=budget,
            cache=str(cache.path) if cache.path is not None else "memory",
        )
        summarised.append(spec.name)
    return SkillSummaryReport(
        summarised=tuple(summarised),
        cached=tuple(cached),
        unavailable=tuple(unavailable),
        failed=tuple(failed),
    )
