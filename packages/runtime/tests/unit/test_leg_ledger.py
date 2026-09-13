"""Reading a leg's ledger out of its finished run (Spec W1, T11; D-W1-16).

The distiller tests prove the ledger accumulates across legs. These test the extraction
itself, because the distiller's own merge deduplicates too: a mutation that broke the
extraction's dedupe survived every test that went through the writer, since the merge
quietly cleaned up after it. Two dedupes in series mean neither is pinned by the other.
"""

from __future__ import annotations

from datetime import UTC, datetime

from persona.schema.tools import ToolCall, ToolResult
from persona_runtime.agentic.run import Run, RunStatus
from persona_runtime.agentic.step import Step, StepType
from persona_runtime.legs.ledger import fold_oldest, queries_from_run, sources_from_run

_NOW = datetime(2026, 9, 12, 9, 0, tzinfo=UTC)


def _step(
    name: str, args: dict[str, object], data: dict[str, object], *, failed: bool = False
) -> Step:
    call = ToolCall(name=name, args=args, call_id="c1")
    result = ToolResult(
        tool_name=name,
        call_id="c1",
        content="upstream 503" if failed else "ok",
        is_error=failed,
        data=data,
    )
    return Step(type=StepType.TOOL_CALL, tool_calls=[call], results=[result])


def _search(query: str, urls: list[str], *, failed: bool = False) -> Step:
    return _step(
        "web_search",
        {"query": query},
        {"results": [{"title": "t", "url": u, "snippet": "s"} for u in urls]},
        failed=failed,
    )


def _run(*steps: Step) -> Run:
    return Run(
        persona_id="p",
        task="x",
        status=RunStatus.COMPLETED,
        steps=list(steps),
        output="done",
        started_at=_NOW,
        finished_at=_NOW,
    )


def test_a_query_asked_twice_in_one_run_is_recorded_once() -> None:
    """The next leg reads one line and skips one query. Recording it twice would spend the
    ledger's small budget saying the same thing."""
    queries = queries_from_run(_run(_search("deposit rules", []), _search("deposit rules", [])))

    assert queries == ("deposit rules",)


def test_distinct_queries_keep_the_order_they_were_asked_in() -> None:
    queries = queries_from_run(
        _run(_search("first", []), _search("second", []), _search("first", []))
    )

    assert queries == ("first", "second")


def test_a_failed_search_is_not_recorded_as_asked() -> None:
    assert queries_from_run(_run(_search("deposit rules", [], failed=True))) == ()


def test_a_search_without_a_query_contributes_nothing() -> None:
    assert queries_from_run(_run(_step("web_search", {}, {}))) == ()


def test_only_search_tools_count_as_queries() -> None:
    """A file read is not a question put to the world, and a later leg re-reading a file is
    exactly what it should do if the file may have changed."""
    assert queries_from_run(_run(_step("file_read", {"query": "x"}, {}))) == ()


def test_the_same_url_seen_twice_is_recorded_once() -> None:
    sources = sources_from_run(
        _run(_search("a", ["https://a.no/x"]), _search("b", ["https://a.no/x", "https://b.no/y"]))
    )

    assert sources == ("https://a.no/x", "https://b.no/y")


def test_a_fetched_page_is_a_source() -> None:
    sources = sources_from_run(
        _run(_step("web_fetch", {"url": "https://c.no/z"}, {"url": "https://c.no/z"}))
    )

    assert sources == ("https://c.no/z",)


def test_a_failed_fetch_is_not_a_source_seen() -> None:
    failed = _step("web_fetch", {"url": "https://c.no/z"}, {"url": "https://c.no/z"}, failed=True)

    assert sources_from_run(_run(failed)) == ()


def test_an_unrecognised_payload_shape_contributes_nothing() -> None:
    """Read from the declared structured payload, never guessed at: a tool whose data looks
    different contributes no sources rather than a parse of someone's prose."""
    assert sources_from_run(_run(_step("web_search", {"query": "q"}, {"hits": ["x"]}))) == ()


def test_folding_keeps_the_newest_and_counts_the_rest() -> None:
    folded = fold_oldest([f"query number {n}" for n in range(50)], target_tokens=30, noun="queries")

    assert folded[0].startswith("[")
    assert "folded" in folded[0]
    assert folded[-1] == "query number 49"
    assert all(entry.strip() == entry for entry in folded)  # never half an entry


def test_folding_leaves_a_small_ledger_alone() -> None:
    entries = ["one query", "another query"]

    assert fold_oldest(entries, target_tokens=1000, noun="queries") == tuple(entries)


def test_a_ledger_with_no_room_at_all_folds_to_nothing() -> None:
    """A zero share is the honest answer to "there is no budget for this", not a crash."""
    assert fold_oldest(["a query"], target_tokens=0, noun="queries") == ()
