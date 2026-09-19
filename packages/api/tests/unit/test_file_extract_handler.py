"""R9-025b — unit tests for the ``file_extract`` A0 tenant ("Turn into file").

Fast, no-DB (the title_refresh/avatar handler test pattern): the handler with
SCRIPTED extractor/renderer/repository fakes. Covers the format-decision
matrix, the bounded prompt/window/episodic excerpting, lenient structured-
output parsing, the sandbox code-template builders (pure string assembly, no
sandbox dependency), and the handler's orchestration (success meters twice +
publishes; a bad extraction / a renderer failure raises FileExtractionError
with the fixed-vocabulary reason; a non-assistant target or a deleted
conversation/message degrades to a graceful no-op; an episodic-recall failure
degrades to conversation-window-only).
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
from pathlib import Path
from typing import Any

import pytest
from persona.backends.types import ChatResponse, TokenUsage
from persona.errors import FileExtractionError
from persona.jobs import MEDIUM_LEASE, JobRegistry, RetryPolicy
from persona_api.background import worker_root
from persona_api.jobs.handlers.file_extract import (
    FILE_EXTRACT_JOB_TYPE,
    ExtractedContent,
    FileExtractContext,
    FileExtractHandler,
    FileExtractJobPayload,
    RenderedFile,
    build_extraction_prompt,
    build_file_extract_generator,
    conversation_window_excerpt,
    decide_format,
    episodic_context_excerpt,
    file_extract_idempotency_key,
    file_extract_queue_ready,
    parse_extracted_content,
    register_file_extract_handler,
    render_sandbox_code,
)
from persona_api.services.llm_usage_collector import UsageCollectingBackend

# ----- fakes -------------------------------------------------------------------


class _Repo:
    """Recording fake of the FileExtractRepository port."""

    def __init__(self, data: FileExtractContext | None) -> None:
        self._data = data
        self.reads: list[tuple[str, str]] = []

    def read(
        self,
        conn: object,  # noqa: ARG002 — Protocol contract; fake doesn't use it
        *,
        conversation_id: str,
        message_id: str,
    ) -> FileExtractContext | None:
        self.reads.append((conversation_id, message_id))
        return self._data


class _Channel:
    """Duck-typed UserEventChannel: records every publish."""

    def __init__(self) -> None:
        self.published: list[tuple[str, object]] = []

    def publish(self, owner_id: str, event: object) -> None:
        self.published.append((owner_id, event))


class _Ctx:
    """Duck-typed JobContext: owner-scoped connection + meter recording."""

    def __init__(self) -> None:
        self.owner_id = "u1"
        self.job_id = "job_1"
        self.metered: list[dict[str, Any]] = []

    def connection(self) -> contextlib.AbstractContextManager[object]:
        return contextlib.nullcontext(object())

    def meter(self, **kwargs: Any) -> None:  # noqa: ANN401
        self.metered.append(kwargs)


def _content(
    *, kind: str = "prose", title: str = "Board game night plan", body: str = "Bring snacks."
) -> ExtractedContent:
    return ExtractedContent(title=title, kind=kind, body_markdown=body)  # type: ignore[arg-type]


class _ExtractorStub:
    """Scripted extractor — returns a fixed ExtractedContent (or None), records prompts."""

    def __init__(self, result: ExtractedContent | None) -> None:
        self._result = result
        self.calls: list[list[object]] = []

    async def __call__(self, prompt: list[object]) -> ExtractedContent | None:
        self.calls.append(prompt)
        return self._result


class _RendererStub:
    """Scripted renderer — returns a fixed RenderedFile or raises, records calls."""

    def __init__(self, result: RenderedFile | None = None, error: Exception | None = None) -> None:
        self._result = result
        self._error = error
        self.calls: list[dict[str, object]] = []

    async def render(
        self,
        content: ExtractedContent,
        *,
        format: str,  # noqa: A002
        owner_id: str,
        persona_id: str,
        conversation_id: str,
        message_id: str,
    ) -> RenderedFile:
        self.calls.append(
            {
                "content": content,
                "format": format,
                "owner_id": owner_id,
                "persona_id": persona_id,
                "conversation_id": conversation_id,
                "message_id": message_id,
            }
        )
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


def _data(
    *,
    persona_id: str = "persona_1",
    role: str = "assistant",
    content: str = "Here's the plan: weekly board games, rotating hosts.",
    window: tuple[tuple[str, str], ...] = (),
) -> FileExtractContext:
    return FileExtractContext(
        persona_id=persona_id, target_role=role, target_content=content, window=window
    )


def _rendered(*, format: str = "pdf") -> RenderedFile:  # noqa: A002
    return RenderedFile(
        workspace_path=f"uploads/board-game-night-plan.{format}",
        media_type="application/pdf" if format == "pdf" else "text/plain",
        size_bytes=1234,
        format=format,
    )


def _run(handler: FileExtractHandler, ctx: _Ctx | None = None) -> _Ctx:
    context = ctx or _Ctx()
    payload = FileExtractJobPayload(conversation_id="conv_1", message_id="msg_1", format="auto")
    asyncio.run(handler.handle(payload, context))  # type: ignore[arg-type]
    return context


# ----- format-decision matrix (the auto heuristic) ------------------------------


@pytest.mark.parametrize(
    ("requested", "kind", "expected"),
    [
        ("auto", "tabular", "xlsx"),
        ("auto", "prose", "pdf"),
        ("pdf", "tabular", "pdf"),  # explicit always wins over content shape
        ("pdf", "prose", "pdf"),
        ("md", "tabular", "md"),
        ("md", "prose", "md"),
        ("xlsx", "prose", "xlsx"),
        ("xlsx", "tabular", "xlsx"),
        ("csv", "prose", "csv"),
        ("csv", "tabular", "csv"),
    ],
)
def test_decide_format_matrix(requested: str, kind: str, expected: str) -> None:
    assert decide_format(requested, kind) == expected  # type: ignore[arg-type]


# ----- bounded excerpting --------------------------------------------------------


def test_conversation_window_excerpt_renders_role_text_lines() -> None:
    excerpt = conversation_window_excerpt((("user", "hi"), ("assistant", "hello")))
    assert excerpt == "user: hi\nassistant: hello"


def test_conversation_window_excerpt_truncates_each_message() -> None:
    excerpt = conversation_window_excerpt((("user", "x" * 2000),))
    (line,) = excerpt.splitlines()
    assert len(line) <= len("user: ") + 1200 + 1
    assert line.endswith("…")


def test_conversation_window_excerpt_collapses_whitespace() -> None:
    excerpt = conversation_window_excerpt((("user", "a\n\n  b\t c"),))
    assert excerpt == "user: a b c"


def test_episodic_context_excerpt_empty_is_empty_string() -> None:
    assert episodic_context_excerpt(()) == ""


def test_episodic_context_excerpt_renders_bullets_and_truncates() -> None:
    excerpt = episodic_context_excerpt(("likes board games", "y" * 1000))
    lines = excerpt.splitlines()
    assert lines[0] == "- likes board games"
    assert lines[1].startswith("- ")
    assert len(lines[1]) <= len("- ") + 400 + 1
    assert lines[1].endswith("…")


def test_build_extraction_prompt_bounds_to_the_supplied_sections() -> None:
    prompt = build_extraction_prompt(
        target_content="Weekly board games it is.",
        window_excerpt="user: let's plan\nassistant: sure",
        episodic_excerpt="- likes strategy games",
    )
    assert len(prompt) == 2
    system, user = prompt
    assert system.role == "system"
    assert user.role == "user"
    assert "Weekly board games it is." in user.content
    assert "user: let's plan" in user.content
    assert "likes strategy games" in user.content


def test_build_extraction_prompt_omits_empty_optional_sections() -> None:
    prompt = build_extraction_prompt(
        target_content="Just this.", window_excerpt="", episodic_excerpt=""
    )
    _system, user = prompt
    assert "CONVERSATION WINDOW" not in user.content
    assert "RECALLED MEMORY" not in user.content
    assert "Just this." in user.content


# ----- lenient structured-output parsing -----------------------------------------


def test_parse_extracted_content_valid_json() -> None:
    raw = (
        '{"kind": "prose", "title": "Board game night", '
        '"body_markdown": "Weekly, rotating hosts.", "columns": [], "rows": []}'
    )
    parsed = parse_extracted_content(raw)
    assert parsed is not None
    assert parsed.kind == "prose"
    assert parsed.title == "Board game night"


def test_parse_extracted_content_strips_code_fence() -> None:
    raw = (
        "```json\n"
        '{"kind": "tabular", "title": "Prices", "body_markdown": "caption", '
        '"columns": ["Item", "Price"], "rows": [["Tea", "3"]]}\n'
        "```"
    )
    parsed = parse_extracted_content(raw)
    assert parsed is not None
    assert parsed.kind == "tabular"
    assert parsed.columns == ("Item", "Price")
    assert parsed.rows == (("Tea", "3"),)


def test_parse_extracted_content_coerces_numeric_cells() -> None:
    raw = (
        '{"kind": "tabular", "title": "Prices", "body_markdown": "caption", '
        '"columns": ["Item", "Price"], "rows": [["Tea", 3], ["Coffee", 4.5]]}'
    )
    parsed = parse_extracted_content(raw)
    assert parsed is not None
    assert parsed.rows == (("Tea", "3"), ("Coffee", "4.5"))


@pytest.mark.parametrize(
    "raw",
    [
        "not json at all",
        "",
        "[]",  # a JSON array, not an object
        '{"kind": "prose"}',  # missing required title/body_markdown
        '{"kind": "invalid", "title": "x", "body_markdown": "y"}',  # bad enum
        '{"title": "", "kind": "prose", "body_markdown": "y"}',  # empty title
    ],
)
def test_parse_extracted_content_unusable_returns_none(raw: str) -> None:
    assert parse_extracted_content(raw) is None


# ----- sandbox code-template builders (pure string assembly) --------------------


def test_render_sandbox_code_md_writes_title_and_body() -> None:
    content = _content(kind="prose", title="Board Game Night", body="Weekly on Fridays.")
    code = render_sandbox_code(content, "md", "board-game-night")
    assert "/workspace/out/board-game-night.md" in code
    assert "# Board Game Night" in code
    assert "Weekly on Fridays." in code


def test_render_sandbox_code_csv_uses_stdlib_csv_module() -> None:
    content = ExtractedContent(
        title="Prices",
        kind="tabular",
        body_markdown="caption",
        columns=("Item", "Price"),
        rows=(("Tea", "3"), ("Coffee", "4")),
    )
    code = render_sandbox_code(content, "csv", "prices")
    assert "import csv" in code
    assert "/workspace/out/prices.csv" in code
    assert "'Item', 'Price'" in code or '["Item", "Price"]' in code or "Item" in code


def test_render_sandbox_code_csv_falls_back_to_one_column_for_prose() -> None:
    content = _content(kind="prose", body="Paragraph one.\n\nParagraph two.")
    code = render_sandbox_code(content, "csv", "notes")
    assert "Content" in code
    assert "Paragraph one." in code
    assert "Paragraph two." in code


def test_render_sandbox_code_xlsx_uses_openpyxl() -> None:
    content = ExtractedContent(
        title="Prices",
        kind="tabular",
        body_markdown="caption",
        columns=("Item", "Price"),
        rows=(("Tea", "3"),),
    )
    code = render_sandbox_code(content, "xlsx", "prices")
    assert "from openpyxl import Workbook" in code
    assert "/workspace/out/prices.xlsx" in code


def test_render_sandbox_code_pdf_has_reportlab_and_matplotlib_fallback() -> None:
    content = _content(title="Report", body="First finding.\n\nSecond finding.")
    code = render_sandbox_code(content, "pdf", "report")
    assert "reportlab" in code
    assert "ModuleNotFoundError" in code
    assert "matplotlib" in code
    assert "/workspace/out/report.pdf" in code


def test_render_sandbox_code_pdf_escapes_reportlab_markup_chars() -> None:
    content = _content(title="A & B < C", body="x < y & y > z")
    code = render_sandbox_code(content, "pdf", "report")
    # The reportlab branch must see escaped markup (raw '<'/'&' would break
    # Paragraph's mini-XML parser); the matplotlib branch uses the RAW text.
    assert "&amp;" in code
    assert "&lt;" in code


def test_render_sandbox_code_unknown_format_raises() -> None:
    with pytest.raises(ValueError, match="file_extract"):
        render_sandbox_code(_content(), "docx", "x")


# ----- idempotency + queue-ready gate --------------------------------------------


def test_idempotency_key_shape() -> None:
    payload = FileExtractJobPayload(conversation_id="c", message_id="m", format="pdf")
    assert file_extract_idempotency_key(payload) == "file_extract:m:pdf"


@pytest.mark.parametrize(
    ("tier_registry", "sandbox_pool", "expected"),
    [
        (object(), object(), True),
        (None, object(), False),
        (object(), None, False),
        (None, None, False),
    ],
)
def test_file_extract_queue_ready_gate(
    tier_registry: object | None, sandbox_pool: object | None, expected: bool
) -> None:
    assert (
        file_extract_queue_ready(tier_registry=tier_registry, sandbox_pool=sandbox_pool) is expected
    )


# ----- handler orchestration ------------------------------------------------------


def test_success_extracts_renders_meters_twice_and_publishes() -> None:
    repo = _Repo(_data())
    channel = _Channel()
    extractor = _ExtractorStub(_content(kind="prose"))
    renderer = _RendererStub(_rendered(format="pdf"))
    handler = FileExtractHandler(
        extractor=extractor,
        renderer=renderer,
        repository=repo,
        event_channel=channel,  # type: ignore[arg-type]
    )
    ctx = _run(handler)

    assert len(extractor.calls) == 1
    assert len(renderer.calls) == 1
    assert renderer.calls[0]["format"] == "pdf"  # auto + prose -> pdf
    assert renderer.calls[0]["persona_id"] == "persona_1"
    assert renderer.calls[0]["owner_id"] == "u1"

    kinds = [m["kind"] for m in ctx.metered]
    assert kinds == ["model", "sandbox"]
    assert ctx.metered[0]["detail"]["surface"] == "file_extract"

    assert len(channel.published) == 1
    owner, event = channel.published[0]
    assert owner == "u1"
    assert getattr(event, "type", None) == "sidebar.changed"
    assert getattr(event, "reason", None) == "conversation.file_extracted"


def test_tabular_content_with_auto_format_renders_xlsx() -> None:
    repo = _Repo(_data())
    extractor = _ExtractorStub(_content(kind="tabular"))
    renderer = _RendererStub(_rendered(format="xlsx"))
    handler = FileExtractHandler(extractor=extractor, renderer=renderer, repository=repo)
    _run(handler)
    assert renderer.calls[0]["format"] == "xlsx"


def test_explicit_format_overrides_the_auto_heuristic() -> None:
    repo = _Repo(_data())
    extractor = _ExtractorStub(_content(kind="tabular"))  # would auto-pick xlsx
    renderer = _RendererStub(_rendered(format="md"))
    handler = FileExtractHandler(extractor=extractor, renderer=renderer, repository=repo)
    context = _Ctx()
    payload = FileExtractJobPayload(conversation_id="conv_1", message_id="msg_1", format="md")
    asyncio.run(handler.handle(payload, context))  # type: ignore[arg-type]
    assert renderer.calls[0]["format"] == "md"


def test_non_assistant_target_is_a_graceful_noop() -> None:
    # Defensive re-check: the route already 422s at enqueue time; a stale
    # enqueue racing an edit must not extract the wrong thing.
    repo = _Repo(_data(role="user"))
    channel = _Channel()
    extractor = _ExtractorStub(_content())
    renderer = _RendererStub(_rendered())
    handler = FileExtractHandler(
        extractor=extractor,
        renderer=renderer,
        repository=repo,
        event_channel=channel,  # type: ignore[arg-type]
    )
    _run(handler)
    assert extractor.calls == []
    assert renderer.calls == []
    assert channel.published == []


def test_conversation_or_message_gone_is_a_graceful_noop() -> None:
    repo = _Repo(None)
    extractor = _ExtractorStub(_content())
    renderer = _RendererStub(_rendered())
    handler = FileExtractHandler(extractor=extractor, renderer=renderer, repository=repo)
    _run(handler)
    assert extractor.calls == []
    assert renderer.calls == []


def test_unusable_extraction_raises_file_extraction_error_llm_reason() -> None:
    repo = _Repo(_data())
    extractor = _ExtractorStub(None)  # unparseable / empty generation
    renderer = _RendererStub(_rendered())
    handler = FileExtractHandler(extractor=extractor, renderer=renderer, repository=repo)
    with pytest.raises(FileExtractionError) as excinfo:
        _run(handler)
    assert excinfo.value.context.get("reason") == "llm_extraction_failed"
    assert renderer.calls == []  # never reached the render step


def test_renderer_failure_propagates_and_never_publishes() -> None:
    repo = _Repo(_data())
    channel = _Channel()
    extractor = _ExtractorStub(_content())
    render_error = FileExtractionError(
        "sandbox down", context={"reason": "sandbox_execution_failed"}
    )
    renderer = _RendererStub(error=render_error)
    handler = FileExtractHandler(
        extractor=extractor,
        renderer=renderer,
        repository=repo,
        event_channel=channel,  # type: ignore[arg-type]
    )
    with pytest.raises(FileExtractionError) as excinfo:
        _run(handler)
    assert excinfo.value.context.get("reason") == "sandbox_execution_failed"
    assert channel.published == []  # no false "it's ready" ping on a failed render


def test_episodic_recall_failure_degrades_to_window_only() -> None:
    repo = _Repo(_data())
    extractor = _ExtractorStub(_content())
    renderer = _RendererStub(_rendered())

    def _boom(_persona_id: str, _text: str) -> tuple[str, ...]:
        raise RuntimeError("recall backend unavailable")

    handler = FileExtractHandler(
        extractor=extractor, renderer=renderer, repository=repo, episodic_query=_boom
    )
    # Never raises — the extraction still proceeds conversation-window-only.
    _run(handler)
    assert len(extractor.calls) == 1
    assert len(renderer.calls) == 1


def test_episodic_snippets_reach_the_prompt_when_recall_succeeds() -> None:
    repo = _Repo(_data())
    extractor = _ExtractorStub(_content())
    renderer = _RendererStub(_rendered())

    def _recall(_persona_id: str, _text: str) -> tuple[str, ...]:
        return ("the user prefers strategy games",)

    handler = FileExtractHandler(
        extractor=extractor, renderer=renderer, repository=repo, episodic_query=_recall
    )
    _run(handler)
    (prompt,) = extractor.calls
    _system, user = prompt
    assert "strategy games" in user.content  # type: ignore[attr-defined]


# ----- registration ----------------------------------------------------------------


def test_registration_declares_type_payload_key_retry_and_lease() -> None:
    registry = JobRegistry()
    extractor = _ExtractorStub(_content())
    renderer = _RendererStub(_rendered())
    register_file_extract_handler(registry, extractor=extractor, renderer=renderer)  # type: ignore[arg-type]
    assert FILE_EXTRACT_JOB_TYPE in registry.types()
    spec = registry.get(FILE_EXTRACT_JOB_TYPE)
    assert spec.payload_model is FileExtractJobPayload
    payload = FileExtractJobPayload(conversation_id="c", message_id="m", format="auto")
    assert spec.idempotency_key(payload) == "file_extract:m:auto"
    # The job system's PLAIN default retry policy — no override (R9-025b: an
    # LLM/sandbox hiccup is an ordinary transient background failure).
    assert spec.retry == RetryPolicy()
    assert spec.lease == MEDIUM_LEASE


# ----- both of this job's paid surfaces are charged to the owner (M3 T5) ---------


class _BillingBackend:
    """A backend whose one call reports real usage, the way a served model does."""

    provider_name = "openrouter"
    model_name = "m"
    supports_native_tools = False
    supports_vision = False

    def __init__(
        self, content: str = '{"title": "T", "kind": "prose", "body_markdown": "B"}'
    ) -> None:
        self._content = content

    async def chat(self, messages: object, **_kw: object) -> ChatResponse:  # noqa: ARG002
        return ChatResponse(
            content=self._content,
            usage=TokenUsage(
                prompt_tokens=2000, completion_tokens=300, total_tokens=2300, cost_usd=0.02
            ),
            model="m",
            provider="openrouter",
            latency_ms=1.0,
        )


class _RecordingPolicy:
    def __init__(self) -> None:
        self.charges: list[dict[str, Any]] = []

    def capture_up_to_idempotent(self, **kw: Any) -> tuple[int, int]:  # noqa: ANN401
        self.charges.append(kw)
        return int(kw["amount"]), 100


def _billing_handler(
    policy: _RecordingPolicy,
    *,
    content: str = '{"title": "T", "kind": "prose", "body_markdown": "B"}',
    renderer: _RendererStub | None = None,
) -> FileExtractHandler:
    backend = UsageCollectingBackend(_BillingBackend(content))  # type: ignore[arg-type]
    return FileExtractHandler(
        extractor=build_file_extract_generator(backend),
        renderer=renderer or _RendererStub(_rendered(format="pdf")),  # type: ignore[arg-type]
        repository=_Repo(_data()),  # type: ignore[arg-type]
        credits_policy=policy,  # type: ignore[arg-type]
        rls_engine=object(),  # type: ignore[arg-type]
    )


def test_a_file_extract_bills_its_model_call_to_the_owner() -> None:
    """The extraction is a real model call on the owner's behalf with nobody in the loop,
    and it was billed to nobody: the handler never reached bill_background_llm, and the
    worker root composed its backend unmetered so there was no usage to bill from either."""
    policy = _RecordingPolicy()

    _run(_billing_handler(policy))

    model_charges = [c for c in policy.charges if c["reason"].startswith("file_extract:")]
    assert len(model_charges) == 1, "the file_extract model call was not billed"
    assert model_charges[0]["cost_cents"] == pytest.approx(2.0)  # 0.02 USD actual
    assert model_charges[0]["cost_basis"] == "actual_openrouter"
    assert model_charges[0]["billing_key"] == "file_extract:msg_1:auto"


def test_an_unusable_extraction_is_still_billed() -> None:
    """The model call HAPPENED and cost money whether or not its output could be parsed;
    the reject branch is exactly where that cost would otherwise vanish."""
    policy = _RecordingPolicy()

    with pytest.raises(FileExtractionError):
        _run(_billing_handler(policy, content="not json at all"))

    assert [c["reason"].split(":")[0] for c in policy.charges] == ["file_extract"]


def test_the_sandbox_render_is_billed_as_per_execution_infra() -> None:
    """The renderer drives the sandbox inside a background job, so no request context is
    bound and no task leg is listening: after fb1ad05c its execution was charged to nobody
    at all. It rides infra_flat_cents, the per-execution infra parameter of the one formula."""
    policy = _RecordingPolicy()

    _run(_billing_handler(policy))

    sandbox = [c for c in policy.charges if c["reason"] == "file_extract_sandbox:infra_flat"]
    assert len(sandbox) == 1, "the sandbox render was not billed"
    assert sandbox[0]["cost_basis"] == "infra_flat"
    assert sandbox[0]["cost_cents"] == pytest.approx(0.0)  # infra, no provider cost
    assert sandbox[0]["amount"] >= 1


def test_the_two_charges_use_different_keys_under_the_one_job_key() -> None:
    """The conflict gate is unique on billing_key alone, so the same key for both would let
    the model charge's claim row swallow the sandbox charge and the render would be free."""
    policy = _RecordingPolicy()

    _run(_billing_handler(policy))

    keys = [c["billing_key"] for c in policy.charges]
    assert keys == ["file_extract:msg_1:auto", "file_extract:msg_1:auto:sandbox"]


def test_a_redelivered_job_reuses_both_keys() -> None:
    """A retry re-runs the model and the render and must hit the same two conflict gates."""
    policy = _RecordingPolicy()

    _run(_billing_handler(policy))
    _run(_billing_handler(policy))

    assert len(policy.charges) == 4
    assert len({c["billing_key"] for c in policy.charges}) == 2


def test_a_failed_render_bills_the_model_but_not_the_sandbox() -> None:
    """Nothing was produced, so there is no successful execution to charge for; this mirrors
    the tool's own hook, which fires on outcome == "ok" only."""
    policy = _RecordingPolicy()
    renderer = _RendererStub(error=FileExtractionError("boom", context={"reason": "x"}))

    with pytest.raises(FileExtractionError):
        _run(_billing_handler(policy, renderer=renderer))

    assert [c["reason"].split(":")[0] for c in policy.charges] == ["file_extract"]


def test_billing_never_fails_the_extraction() -> None:
    """Fail-soft: the file is the deliverable, the charge is enrichment."""

    class _ExplodingPolicy(_RecordingPolicy):
        def capture_up_to_idempotent(self, **kw: Any) -> tuple[int, int]:  # noqa: ANN401, ARG002
            msg = "ledger down"
            raise RuntimeError(msg)

    ctx = _run(_billing_handler(_ExplodingPolicy()))

    assert [m["kind"] for m in ctx.metered] == ["model", "sandbox"]


def test_an_unmetered_install_charges_nothing() -> None:
    """No credits policy (community / self-host) leaves the job byte-identical to before."""
    handler = FileExtractHandler(
        extractor=build_file_extract_generator(UsageCollectingBackend(_BillingBackend())),  # type: ignore[arg-type]
        renderer=_RendererStub(_rendered(format="pdf")),  # type: ignore[arg-type]
        repository=_Repo(_data()),  # type: ignore[arg-type]
    )

    ctx = _run(handler)

    assert [m["kind"] for m in ctx.metered] == ["model", "sandbox"]


def test_the_worker_root_meters_file_extract_and_passes_a_credits_policy() -> None:
    """The composition half. Both conditions had to hold and neither did, and a behavioural
    test cannot see the second one: the handler could ask to bill all it liked while the root
    composed the backend with metered=False, and the charge would fall back to the floor."""
    tree = ast.parse(Path(worker_root.__file__).read_text(encoding="utf-8"))

    metered: list[bool] = []
    policies: list[bool] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id == "plan_scoped_background_backend":
            for kw in node.keywords:
                if kw.arg == "tier" and "file_extract_tier" in ast.dump(kw.value):
                    metered.append(
                        any(
                            k.arg == "metered"
                            and isinstance(k.value, ast.Constant)
                            and k.value.value is True
                            for k in node.keywords
                        )
                    )
        if node.func.id == "register_file_extract_handler":
            policies.append("credits_policy" in {kw.arg for kw in node.keywords})

    assert metered == [True], "the file_extract backend is not metered; its usage cannot be billed"
    assert policies == [True], "the file_extract handler is registered without a credits policy"
