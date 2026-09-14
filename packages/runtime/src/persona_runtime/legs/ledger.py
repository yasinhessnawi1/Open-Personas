"""What a leg asked the world, carried to the next leg (Spec W1, T11; D-W1-16).

A task runs as many bounded legs. Leg 1 searched "husleieloven deposit interest", read three
pages and concluded something; leg 2 starts from the checkpoint, sees the conclusion, and has
no idea that query was ever run, so it runs it again, reads the same three pages again, and
pays for both. Across a week of legs that is most of what a recurring task does.

The checkpoint already carries conclusions between legs. These two ledgers carry the
BOOKKEEPING beside them: the searches that were run and the sources that were read. Both are
extracted from the finished run rather than asked of the model, because a model that is
asked to keep its own ledger writes one when it remembers to.

Bounded like everything else in the accumulating core (D-A2-1): entries are deduplicated,
oldest-first, and when the ledger outgrows its share of the budget the oldest fold into a
single count marker. Never truncated mid-entry: half a URL is worse than an honest count of
the ones that no longer fit.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from persona.skills import count_tokens
from persona.tasks import pointers_from_artifacts

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from persona.tasks import ArtifactPointer

    from persona_runtime.agentic.run import Run

__all__ = [
    "LEDGER_TOKEN_SHARE",
    "SEARCH_TOOLS",
    "SOURCE_TOOLS",
    "artifacts_from_run",
    "fold_oldest",
    "queries_from_run",
    "sources_from_run",
]

#: The fraction of the checkpoint budget each ledger may occupy. Small on purpose: the
#: ledgers exist to stop repeats, and a list of what was already asked is worth far less
#: per token than a conclusion. Two ledgers at a tenth each leave the conclusions four
#: fifths of the core.
LEDGER_TOKEN_SHARE: Final = 0.1

#: Tools whose arguments ARE a question put to the world, so repeating one costs money and
#: returns what the run already has.
SEARCH_TOOLS: Final[frozenset[str]] = frozenset({"web_search"})

#: Tools whose results name a source that was actually read.
SOURCE_TOOLS: Final[frozenset[str]] = frozenset({"web_search", "web_fetch"})

#: How much of one entry survives. A query or a URL past this is almost certainly a
#: generated blob rather than something a later leg would recognise and skip.
_MAX_ENTRY_CHARS: Final = 300


def queries_from_run(run: Run) -> tuple[str, ...]:
    """The searches this run ANSWERED, in order, deduplicated.

    A search that failed is deliberately absent. The ledger is read by the next leg as "this
    has been asked, do not ask it again", and that is only true of a search that came back
    with something. A search that hit a rate limit or a timeout was never really asked, and
    recording it would let one bad minute cost a task a source for the rest of its life.
    Within a run the repeat guard already refuses the verbatim retry (D-W1-11); across legs
    the world has moved on.
    """
    queries: list[str] = []
    for step in run.steps:
        for call, result in zip(step.tool_calls, step.results, strict=False):
            if call.name not in SEARCH_TOOLS or result.is_error:
                continue
            query = str(call.args.get("query", "")).strip()
            if query:
                queries.append(query[:_MAX_ENTRY_CHARS])
    return _dedupe(queries)


def sources_from_run(run: Run) -> tuple[str, ...]:
    """The URLs this run's tool results named, in order, deduplicated.

    Read from the STRUCTURED payload (``data``), never parsed out of the prose: the content
    is written for the model and its shape is a provider's business, while ``data`` is the
    contract the tools declare (D-03-3).
    """
    sources: list[str] = []
    for step in run.steps:
        for result in step.results:
            if result.is_error or result.tool_name not in SOURCE_TOOLS:
                continue
            sources.extend(_urls_in(result.data))
    return _dedupe(sources)


def artifacts_from_run(run: Run) -> tuple[ArtifactPointer, ...]:
    """The files this run actually persisted, as checkpoint pointers, in order (Spec 28 → A2).

    Read from :attr:`persona.schema.tools.ToolResult.artifacts`, the typed channel a
    byte-producing tool surfaces its persisted output on. That is the only honest source: the
    prose content is written for the model, and a path parsed out of it is a guess, while an
    entry here means the bytes are in the workspace under that path.

    A failed result contributes nothing. A tool that errored may still have written a partial
    file, and a pointer to one is worse than no pointer: the next leg would open it and treat
    half a file as the leg's work.
    """
    produced = [
        artifact
        for step in run.steps
        for result in step.results
        if not result.is_error
        for artifact in result.artifacts
    ]
    return pointers_from_artifacts(produced)


def fold_oldest(entries: Sequence[str], *, target_tokens: int, noun: str) -> tuple[str, ...]:
    """Keep the newest entries that fit, replacing the rest with one count marker.

    The ``_compact`` idiom the checkpoint distiller already uses for conclusions: recency
    survives verbatim, the dropped prefix becomes ``[N earlier queries folded]``, and no
    entry is ever cut in half.
    """
    kept = list(entries)
    if target_tokens <= 0:
        return ()
    if count_tokens(" ".join(kept)) <= target_tokens:
        return tuple(kept)
    folded = 0
    while kept and count_tokens(" ".join(kept)) > target_tokens:
        kept.pop(0)
        folded += 1
    if folded:
        return (f"[{folded} earlier {noun} folded]", *kept)
    return tuple(kept)


def _dedupe(entries: Iterable[str]) -> tuple[str, ...]:
    """Order-preserving dedupe: the first time something was asked is when it was asked."""
    seen: dict[str, None] = {}
    for entry in entries:
        seen.setdefault(entry, None)
    return tuple(seen)


def _urls_in(data: object) -> list[str]:
    """The URLs a tool result's structured payload declares.

    ``web_search`` carries ``data["results"] = [{title, url, snippet}, ...]``; ``web_fetch``
    carries ``data["url"]``. Anything else shaped differently contributes nothing rather
    than guessing.
    """
    if not isinstance(data, dict):
        return []
    urls: list[str] = []
    single = data.get("url")
    if isinstance(single, str) and single:
        urls.append(single[:_MAX_ENTRY_CHARS])
    results = data.get("results")
    if isinstance(results, list):
        for item in results:
            if isinstance(item, dict):
                url = item.get("url")
                if isinstance(url, str) and url:
                    urls.append(url[:_MAX_ENTRY_CHARS])
    return urls
