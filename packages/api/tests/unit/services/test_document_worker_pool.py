"""The document parse worker pool, driven with real worker processes (D-FV0-31).

Every test here starts real workers through the real pool, with limits lowered where a
test needs one to trip quickly. Hostile files come from ``_hostile_documents``: each
passes the parsers' own bounds and is stopped only by a worker limit.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
from _hostile_documents import flate_bomb_pdf, genuine, paragraph_flood_docx
from persona.documents.errors import (
    CorruptDocumentError,
    MissingDependencyError,
    UnsupportedFormatError,
)
from persona_api.document_worker import pool as pool_module
from persona_api.document_worker.pool import (
    WORKER_ENV_ALLOWLIST,
    DocumentParseBudgetError,
    DocumentParseCooldownError,
    DocumentParserBusyError,
    DocumentUploadInProgressError,
    ProcessDocumentParser,
    worker_environment,
)

_LINUX = sys.platform.startswith("linux")
_POSIX = os.name == "posix"


def _pool(**overrides: Any) -> ProcessDocumentParser:  # noqa: ANN401
    settings: dict[str, Any] = {"workers": 1}
    settings.update(overrides)
    return ProcessDocumentParser(**settings)


async def _parse(
    pool: ProcessDocumentParser,
    path: Path,
    *,
    owner: str = "u1",
    purpose: str = "upload",
    render_dir: Path | None = None,
) -> Any:  # noqa: ANN401
    return await pool.parse(path, render_dir=render_dir, owner_id=owner, purpose=purpose)  # type: ignore[arg-type]


_TESTS_DIR = Path(__file__).resolve().parents[2]


@pytest.fixture
def fake_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the misbehaving stand-in worker (``_fake_document_worker``) through the real pool."""
    monkeypatch.setattr(pool_module, "WORKER_MODULE", "_fake_document_worker")
    monkeypatch.setenv("PYTHONPATH", str(_TESTS_DIR))


def _mode(tmp_path: Path, mode: str, name: str = "upload.pdf") -> Path:
    path = tmp_path / name
    path.write_text(mode, encoding="utf-8")
    return path


def _pages(tmp_path: Path) -> Path:
    render = tmp_path / "pages"
    render.mkdir(exist_ok=True)
    return render


@pytest.fixture
def bomb(tmp_path: Path) -> Path:
    return flate_bomb_pdf(tmp_path / "bomb.pdf")


@pytest.fixture
def memo(tmp_path: Path) -> Path:
    path = tmp_path / "memo.txt"
    path.write_text("The lease runs for twelve months.", encoding="utf-8")
    return path


class TestGenuineFilesParse:
    """Valid input passes: genuine files parse in a worker under the production limits."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("fixture", ["sample.docx", "sample.xlsx", "sample-text.pdf"])
    async def test_a_genuine_office_or_pdf_file_parses(self, tmp_path: Path, fixture: str) -> None:
        pool = _pool()
        try:
            parsed = await _parse(pool, genuine(tmp_path / fixture, fixture))
        finally:
            await pool.aclose()
        assert parsed.parse_result.full_text.strip()
        assert parsed.page_images == ()

    @pytest.mark.asyncio
    async def test_a_scanned_pdf_renders_its_pages_in_the_worker(self, tmp_path: Path) -> None:
        pages = tmp_path / "pages"
        pages.mkdir()
        pool = _pool()
        try:
            parsed = await _parse(
                pool, genuine(tmp_path / "scan.pdf", "scanned-like.pdf"), render_dir=pages
            )
        finally:
            await pool.aclose()
        assert parsed.parse_result.needs_vision_handoff
        assert [p.name for p in parsed.page_images] == [
            "page-0001.png",
            "page-0002.png",
            "page-0003.png",
        ]
        assert all(p.parent == pages and p.stat().st_size > 0 for p in parsed.page_images)

    @pytest.mark.asyncio
    async def test_a_document_at_the_text_layer_flags_round_trips_exactly(
        self, tmp_path: Path
    ) -> None:
        """What crosses the pipe is the same ParseResult an in-process parse gives."""
        from persona_api.document_worker.untrusted import parse_untrusted_document  # noqa: PLC0415

        source = genuine(tmp_path / "sample.xlsx", "sample.xlsx")
        pool = _pool()
        try:
            parsed = await _parse(pool, source)
        finally:
            await pool.aclose()
        assert parsed.parse_result == parse_untrusted_document(source, render_dir=None).parse_result


class TestAHostileFileKillsOnlyItsWorker:
    @pytest.mark.asyncio
    async def test_past_the_deadline_the_worker_is_killed_and_the_next_upload_works(
        self, bomb: Path, memo: Path
    ) -> None:
        pool = _pool(deadline_seconds=2)
        try:
            with pytest.raises(CorruptDocumentError) as caught:
                await _parse(pool, bomb)
            assert caught.value.context == {
                "format": "pdf",
                "reason": "parse_limit",
                "limit": "deadline",
            }
            assert pool.worker_pids == ()
            # The next upload works; the hostile file's owner is cooling down (F2).
            parsed = await _parse(pool, memo, owner="u2")
            assert parsed.parse_result.full_text == "The lease runs for twelve months."
            with pytest.raises(DocumentParseCooldownError):
                await _parse(pool, memo, owner="u1")
        finally:
            await pool.aclose()

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _POSIX, reason="RLIMIT_CPU needs the resource module (POSIX)")
    async def test_past_the_cpu_limit_the_worker_is_killed(self, bomb: Path, memo: Path) -> None:
        pool = _pool(cpu_seconds=1, deadline_seconds=60)
        try:
            with pytest.raises(CorruptDocumentError) as caught:
                await _parse(pool, bomb)
            assert caught.value.context["limit"] == "cpu_limit"
            assert (await _parse(pool, memo, owner="u2")).parse_result.full_text
        finally:
            await pool.aclose()

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _LINUX, reason="RLIMIT_AS is enforced on Linux only")
    async def test_past_the_memory_limit_the_parse_is_refused(self, bomb: Path, memo: Path) -> None:
        pool = _pool(memory_limit_bytes=200 * 1024 * 1024, deadline_seconds=60)
        try:
            with pytest.raises(CorruptDocumentError) as caught:
                await _parse(pool, bomb)
            assert caught.value.context["limit"] == "memory_limit"
            # A worker that hit MemoryError is never reused: its heap may be in any state.
            assert pool.worker_pids == ()
            assert (await _parse(pool, memo, owner="u2")).parse_result.full_text
        finally:
            await pool.aclose()

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _LINUX, reason="RLIMIT_AS is enforced on Linux only")
    async def test_the_production_memory_limit_lets_the_largest_legal_dom_through(
        self, tmp_path: Path
    ) -> None:
        """Valid input passes: the biggest docx the archive guard allows fits in 1 GiB."""
        flood = paragraph_flood_docx(tmp_path / "flood.docx")
        pool = _pool()
        try:
            parsed = await _parse(pool, flood)
        finally:
            await pool.aclose()
        assert parsed.parse_result.full_text

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _LINUX, reason="RLIMIT_AS is enforced on Linux only")
    async def test_a_lower_memory_limit_refuses_that_same_docx(self, tmp_path: Path) -> None:
        """lxml reports a failed allocation as a syntax error, so the refusal says corrupt."""
        flood = paragraph_flood_docx(tmp_path / "flood.docx")
        pool = _pool(memory_limit_bytes=160 * 1024 * 1024)
        try:
            with pytest.raises(CorruptDocumentError):
                await _parse(pool, flood)
        finally:
            await pool.aclose()


class TestTheCpuAllowanceIsPerJob:
    @pytest.mark.asyncio
    @pytest.mark.skipif(not _POSIX, reason="RLIMIT_CPU needs the resource module (POSIX)")
    async def test_one_worker_serves_more_cpu_in_total_than_one_job_may_use(
        self, tmp_path: Path
    ) -> None:
        """RLIMIT_CPU counts a process's whole life, so the allowance is re-armed per job.

        Five genuine 20,000-row ledgers take about 0.7 s of CPU each: 3.5 s in one worker,
        each job well inside a 2 s allowance.
        """
        import openpyxl  # noqa: PLC0415

        workbook = openpyxl.Workbook(write_only=True)
        sheet = workbook.create_sheet("Ledger")
        sheet.append(["id", "amount", "note"])
        for row in range(20_000):
            sheet.append([row, row * 1.5, f"entry {row}"])
        ledger = tmp_path / "ledger.xlsx"
        workbook.save(ledger)
        pool = _pool(cpu_seconds=2, deadline_seconds=60)
        try:
            pids = set()
            for _ in range(5):
                parsed = await _parse(pool, ledger)
                assert "entry 19999" in parsed.parse_result.full_text
                pids.update(pool.worker_pids)
        finally:
            await pool.aclose()
        assert len(pids) == 1, "precondition: one worker served every job"


class TestBoundedWaiting:
    """The ruling's change (A): a busy pool queues briefly, then refuses with 503."""

    @pytest.mark.asyncio
    async def test_a_parse_waits_for_the_busy_worker_and_then_runs(
        self, bomb: Path, memo: Path
    ) -> None:
        pool = _pool(deadline_seconds=1.5, upload_wait_seconds=30)
        try:
            hostile = asyncio.create_task(_parse(pool, bomb, owner="u1"))
            await asyncio.sleep(0.3)
            parsed = await _parse(pool, memo, owner="u2")
            assert parsed.parse_result.full_text
            with pytest.raises(CorruptDocumentError):
                await hostile
        finally:
            await pool.aclose()

    @pytest.mark.asyncio
    async def test_a_wait_past_the_limit_is_refused_with_a_retry_after(
        self, bomb: Path, memo: Path
    ) -> None:
        pool = _pool(deadline_seconds=5, upload_wait_seconds=0.5, retry_after_seconds=7)
        try:
            hostile = asyncio.create_task(_parse(pool, bomb, owner="u1"))
            await asyncio.sleep(0.3)
            with pytest.raises(DocumentParserBusyError) as caught:
                await _parse(pool, memo, owner="u2")
            assert caught.value.context == {"reason": "wait_expired", "retry_after_seconds": "7"}
            hostile.cancel()
        finally:
            await pool.aclose()

    @pytest.mark.asyncio
    async def test_a_full_queue_refuses_at_once(self, bomb: Path, memo: Path) -> None:
        pool = _pool(deadline_seconds=5, queue_depth=1, upload_wait_seconds=30)
        try:
            hostile = asyncio.create_task(_parse(pool, bomb, owner="u1"))
            await asyncio.sleep(0.3)
            waiting = asyncio.create_task(_parse(pool, memo, owner="u2"))
            await asyncio.sleep(0.1)
            loop = asyncio.get_running_loop()
            started = loop.time()
            with pytest.raises(DocumentParserBusyError) as caught:
                await _parse(pool, memo, owner="u3")
            assert caught.value.context["reason"] == "queue_full"
            assert loop.time() - started < 0.5
            # The one that queued still gets its turn once the hostile file is killed.
            assert (await waiting).parse_result.full_text
            with pytest.raises(CorruptDocumentError):
                await hostile
        finally:
            await pool.aclose()

    @pytest.mark.asyncio
    async def test_a_queue_at_its_depth_still_takes_the_last_place(
        self, bomb: Path, memo: Path
    ) -> None:
        """The depth is a count of waiters allowed, crossed at depth + 1 above."""
        pool = _pool(deadline_seconds=3, queue_depth=2, upload_wait_seconds=30)
        try:
            hostile = asyncio.create_task(_parse(pool, bomb, owner="u1"))
            await asyncio.sleep(0.3)
            first = asyncio.create_task(_parse(pool, memo, owner="u2"))
            second = asyncio.create_task(_parse(pool, memo, owner="u3"))
            results = await asyncio.gather(first, second)
            assert all(r.parse_result.full_text for r in results)
            with pytest.raises(CorruptDocumentError):
                await hostile
        finally:
            await pool.aclose()

    @pytest.mark.asyncio
    async def test_a_re_parse_waits_less_than_an_upload(self, bomb: Path, memo: Path) -> None:
        pool = _pool(deadline_seconds=5, upload_wait_seconds=30, reparse_wait_seconds=0.3)
        try:
            hostile = asyncio.create_task(_parse(pool, bomb, owner="u1"))
            await asyncio.sleep(0.3)
            with pytest.raises(DocumentParserBusyError):
                await _parse(pool, memo, owner="u2", purpose="reparse")
            hostile.cancel()
        finally:
            await pool.aclose()


class TestOneUploadPerUser:
    @pytest.mark.asyncio
    @pytest.mark.usefixtures("fake_worker")
    async def test_a_second_concurrent_upload_from_the_same_user_is_refused(
        self, tmp_path: Path
    ) -> None:
        pool = _pool(workers=2)
        slow = _mode(tmp_path, "slow:1.5", "slow.pdf")
        echo = _mode(tmp_path, "echo", "echo.pdf")
        try:
            first = asyncio.create_task(_parse(pool, slow, owner="u1"))
            await asyncio.sleep(0.5)
            with pytest.raises(DocumentUploadInProgressError):
                await _parse(pool, echo, owner="u1")
            # Another user is not held back, and neither is this user's re-parse.
            assert (await _parse(pool, echo, owner="u2")).parse_result.full_text
            reparsed = await _parse(pool, echo, owner="u1", purpose="reparse")
            assert reparsed.parse_result.full_text
            assert (await first).parse_result.full_text
            # Once the first upload is over, the user may upload again.
            assert (await _parse(pool, echo, owner="u1")).parse_result.full_text
        finally:
            await pool.aclose()


class TestOneOwnerCannotHoldTheWorker:
    """F2: worker time per owner, a cooldown after a limit, and owners served in turn."""

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("fake_worker")
    async def test_each_job_charges_its_owner_the_cpu_seconds_it_used(self, tmp_path: Path) -> None:
        pool = _pool()
        try:
            await _parse(pool, _mode(tmp_path, "spin:0.5"), owner="u1")
        finally:
            await pool.aclose()
        assert 0.45 <= pool.admission.worker_seconds_used("u1") < 5
        assert pool.admission.worker_seconds_used("u2") == 0

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("fake_worker")
    async def test_past_the_budget_uploads_and_re_parses_are_refused(self, tmp_path: Path) -> None:
        from persona_api.document_worker.admission import DocumentAdmission  # noqa: PLC0415

        pool = _pool(admission=DocumentAdmission(worker_seconds_budget=0.5))
        slow = _mode(tmp_path, "spin:0.6", "slow.pdf")
        echo = _mode(tmp_path, "echo", "echo.pdf")
        try:
            assert (await _parse(pool, slow, owner="u1")).parse_result.full_text
            with pytest.raises(DocumentParseBudgetError):
                await _parse(pool, echo, owner="u1")
            with pytest.raises(DocumentParseBudgetError):  # the re-parse is not exempt
                await _parse(pool, echo, owner="u1", purpose="reparse")
            assert (await _parse(pool, echo, owner="u2")).parse_result.full_text
        finally:
            await pool.aclose()

    @pytest.mark.asyncio
    async def test_a_cooling_owner_cannot_re_parse_either(self, bomb: Path, memo: Path) -> None:
        pool = _pool(deadline_seconds=1)
        try:
            with pytest.raises(CorruptDocumentError):
                await _parse(pool, bomb, owner="u1")
            assert pool.admission.is_suspect("u1")
            with pytest.raises(DocumentParseCooldownError):
                await _parse(pool, memo, owner="u1", purpose="reparse")
        finally:
            await pool.aclose()

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("fake_worker")
    async def test_owners_are_served_in_turn_not_in_arrival_order(self, tmp_path: Path) -> None:
        """u1 queues three parses before u2 queues one; u2 goes second, not last."""
        pool = _pool(queue_depth=10, reparse_wait_seconds=30)
        finished: list[str] = []

        async def job(name: str, owner: str, mode: str) -> None:
            await _parse(pool, _mode(tmp_path, mode, f"{name}.pdf"), owner=owner, purpose="reparse")
            finished.append(name)

        try:
            tasks = [asyncio.create_task(job("a0", "u1", "slow:0.4"))]
            await asyncio.sleep(0.2)
            for name in ("a1", "a2", "a3"):
                tasks.append(asyncio.create_task(job(name, "u1", "slow:0.05")))
                await asyncio.sleep(0.01)
            tasks.append(asyncio.create_task(job("b0", "u2", "slow:0.05")))
            await asyncio.gather(*tasks)
        finally:
            await pool.aclose()
        assert finished == ["a0", "a1", "b0", "a2", "a3"]

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("fake_worker")
    async def test_a_cancelled_upload_holds_its_owner_slot_until_the_worker_finishes(
        self, tmp_path: Path
    ) -> None:
        pool = _pool()
        try:
            upload = asyncio.create_task(_parse(pool, _mode(tmp_path, "slow:1.0"), owner="u1"))
            await asyncio.sleep(0.3)
            upload.cancel()
            with pytest.raises(asyncio.CancelledError):
                await upload
            assert pool.admission.upload_in_flight("u1")
            with pytest.raises(DocumentUploadInProgressError):
                await _parse(pool, _mode(tmp_path, "echo", "echo.pdf"), owner="u1")
            await asyncio.sleep(1.2)
            assert not pool.admission.upload_in_flight("u1")
        finally:
            await pool.aclose()


class TestWorkerLifecycle:
    @pytest.mark.asyncio
    async def test_no_worker_starts_until_the_first_parse(self, memo: Path) -> None:
        pool = _pool()
        try:
            assert pool.worker_pids == ()
            await _parse(pool, memo)
            assert len(pool.worker_pids) == 1
        finally:
            await pool.aclose()

    @pytest.mark.asyncio
    async def test_a_worker_is_recycled_after_its_job_allowance(self, memo: Path) -> None:
        pool = _pool(max_jobs_per_worker=2)
        try:
            await _parse(pool, memo)
            (first,) = pool.worker_pids
            await _parse(pool, memo)
            assert pool.worker_pids == ()  # retired after its second job
            await _parse(pool, memo)
            (second,) = pool.worker_pids
            assert second != first
        finally:
            await pool.aclose()

    @pytest.mark.asyncio
    async def test_a_worker_serves_jobs_up_to_its_allowance(self, memo: Path) -> None:
        pool = _pool(max_jobs_per_worker=3)
        try:
            await _parse(pool, memo)
            (first,) = pool.worker_pids
            await _parse(pool, memo)
            assert pool.worker_pids == (first,)
        finally:
            await pool.aclose()

    @pytest.mark.asyncio
    async def test_closing_the_pool_kills_its_workers(self, memo: Path) -> None:
        pool = _pool()
        await _parse(pool, memo)
        (pid,) = pool.worker_pids
        await pool.aclose()
        assert pool.worker_pids == ()
        if _POSIX:
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)

    @pytest.mark.asyncio
    async def test_a_cancelled_request_keeps_the_worker_until_the_job_ends(
        self, bomb: Path, memo: Path
    ) -> None:
        """The slot goes back when the worker is free, not when the caller gives up."""
        pool = _pool(deadline_seconds=2, upload_wait_seconds=0.3)
        try:
            hostile = asyncio.create_task(_parse(pool, bomb, owner="u1"))
            await asyncio.sleep(0.3)
            hostile.cancel()
            with pytest.raises(asyncio.CancelledError):
                await hostile
            with pytest.raises(DocumentParserBusyError):
                await _parse(pool, memo, owner="u2")
            await asyncio.sleep(2.5)  # the deadline kills the hostile job's worker
            assert (await _parse(pool, memo, owner="u2")).parse_result.full_text
        finally:
            await pool.aclose()

    @pytest.mark.asyncio
    async def test_the_event_loop_runs_while_a_worker_parses(self, bomb: Path) -> None:
        """The wait happens on the pool's own thread, never the loop or the default executor."""
        waiter_threads: list[str] = []
        real_run_job = ProcessDocumentParser._run_job

        def _recording_run_job(self: ProcessDocumentParser, *args: Any) -> Any:  # noqa: ANN401
            waiter_threads.append(threading.current_thread().name)
            return real_run_job(self, *args)

        pool = _pool(deadline_seconds=1.5)
        try:
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(ProcessDocumentParser, "_run_job", _recording_run_job)
                hostile = asyncio.create_task(_parse(pool, bomb))
                ticks = 0
                while not hostile.done():
                    await asyncio.sleep(0.01)
                    ticks += 1
            with pytest.raises(CorruptDocumentError):
                await hostile
        finally:
            await pool.aclose()
        assert ticks > 50
        assert len(waiter_threads) == 1
        assert waiter_threads[0].startswith("document-parse-waiter")


class TestOnlyPlainDataCrosses:
    def test_no_secret_reaches_the_worker_environment(self) -> None:
        """Built from the settings' secret fields and from every name .env.example declares."""
        from persona_api.config import APIConfig  # noqa: PLC0415
        from pydantic import SecretStr  # noqa: PLC0415

        secret_names: set[str] = set()
        prefix = str(APIConfig.model_config.get("env_prefix", ""))
        for name, field in APIConfig.model_fields.items():
            annotation = str(field.annotation)
            if field.repr is False or SecretStr.__name__ in annotation:
                alias = field.validation_alias
                secret_names.add(alias if isinstance(alias, str) else f"{prefix}{name}".upper())
        assert {"DATABASE_URL", "APP_DATABASE_URL", "MCP_CREDENTIAL_KEY"} <= secret_names

        example = Path(__file__).resolve().parents[5] / ".env.example"
        declared = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", example.read_text(), re.M))
        assert len(declared) > 100, "precondition: .env.example was read"

        environ = dict.fromkeys(secret_names | declared, "a-secret-value")
        environ.update({"PATH": "/usr/bin", "LANG": "C.UTF-8"})
        passed = worker_environment(environ)
        assert not (secret_names & passed.keys())
        # Of everything .env.example declares, only the two parser tuning knobs cross.
        assert declared & passed.keys() <= {
            "PERSONA_DOC_SPREADSHEET_ROW_CAP",
            "PERSONA_DOC_PDF_NO_TEXT_LAYER_THRESHOLD",
        }
        assert passed.keys() <= WORKER_ENV_ALLOWLIST
        assert {"PATH", "LANG"} <= passed.keys()

    @pytest.mark.asyncio
    async def test_the_worker_is_a_fresh_interpreter_with_only_the_allowlist(
        self, memo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        launches: list[dict[str, Any]] = []
        real_popen = subprocess.Popen

        def _recording_popen(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            launches.append({"argv": list(args[0]), "env": dict(kwargs["env"])})
            return real_popen(*args, **kwargs)

        monkeypatch.setenv("DATABASE_URL", "postgresql://secret@db/persona")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-secret")
        monkeypatch.setattr(pool_module.subprocess, "Popen", _recording_popen)
        pool = _pool()
        try:
            assert (await _parse(pool, memo)).parse_result.full_text
        finally:
            await pool.aclose()
        assert launches
        for launch in launches:  # the job's worker, and the spare if it started
            assert launch["argv"][:4] == [sys.executable, "-P", "-m", pool_module.WORKER_MODULE]
            assert "DATABASE_URL" not in launch["env"]
            assert "ANTHROPIC_API_KEY" not in launch["env"]
            assert set(launch["env"]) <= WORKER_ENV_ALLOWLIST

    def test_the_worker_imports_no_provider_sdk(self) -> None:
        probe = (
            "import sys\n"
            "import persona_api.document_worker.worker\n"
            "import persona_api.document_worker.untrusted\n"
            "heavy = ('anthropic', 'openai', 'httpx', 'tiktoken', 'sqlalchemy', "
            "'persona_api.services', 'persona.skills')\n"
            "print(','.join(m for m in heavy if m in sys.modules))\n"
        )
        run = subprocess.run(
            [sys.executable, "-P", "-c", probe],
            capture_output=True,
            text=True,
            check=True,
            env=worker_environment(os.environ),
        )
        assert run.stdout.strip() == ""


class TestTheWire:
    """The framed JSON between the api and a worker."""

    def test_a_message_round_trips(self) -> None:
        import io  # noqa: PLC0415

        from persona_api.document_worker.protocol import read_frame, write_frame  # noqa: PLC0415

        buffer = io.BytesIO()
        write_frame(buffer, {"path": "/tmp/a.pdf", "text": "fünf"})
        write_frame(buffer, {"kind": "ok"})
        buffer.seek(0)
        assert read_frame(buffer) == {"path": "/tmp/a.pdf", "text": "fünf"}
        assert read_frame(buffer) == {"kind": "ok"}
        assert read_frame(buffer) is None  # a clean end

    def test_a_frame_over_the_limit_is_refused_before_it_is_read(self) -> None:
        import io  # noqa: PLC0415
        import struct  # noqa: PLC0415

        from persona_api.document_worker.protocol import (  # noqa: PLC0415
            MAX_FRAME_BYTES,
            FrameError,
            read_frame,
        )

        at_limit = io.BytesIO(struct.pack(">I", MAX_FRAME_BYTES))
        with pytest.raises(FrameError, match="inside a frame"):
            read_frame(at_limit)  # accepted as a size, then found short
        over = io.BytesIO(struct.pack(">I", MAX_FRAME_BYTES + 1))
        with pytest.raises(FrameError, match="over the frame limit"):
            read_frame(over)

    @pytest.mark.parametrize("body", [b"[1, 2]", b"not json", b"\xff\xfe"])
    def test_a_frame_that_is_not_a_json_object_is_refused(self, body: bytes) -> None:
        import io  # noqa: PLC0415
        import struct  # noqa: PLC0415

        from persona_api.document_worker.protocol import FrameError, read_frame  # noqa: PLC0415

        with pytest.raises(FrameError):
            read_frame(io.BytesIO(struct.pack(">I", len(body)) + body))

    def test_the_frame_cap_is_48_mib(self) -> None:
        """Down from 256 MiB: the largest legal reply plus margin, and no more (F1)."""
        from persona_api.document_worker.protocol import MAX_FRAME_BYTES  # noqa: PLC0415

        assert MAX_FRAME_BYTES == 48 * 1024 * 1024

    def test_the_largest_legal_reply_fits_the_frame_cap(self) -> None:
        """Valid input passes: the whole text budget, at the worst JSON cost per character.

        Four million control characters (6 bytes each as ``\\u00XX``) spread over the
        200,000 sections a docx may have.
        """
        import io  # noqa: PLC0415

        from persona.documents.parsers import MAX_DOCUMENT_TEXT_CHARS  # noqa: PLC0415
        from persona.documents.parsers.docx import MAX_DOCX_BLOCKS  # noqa: PLC0415
        from persona_api.document_worker.protocol import (  # noqa: PLC0415
            MAX_FRAME_BYTES,
            write_frame,
        )

        per_section = MAX_DOCUMENT_TEXT_CHARS // MAX_DOCX_BLOCKS
        reply = {
            "kind": "ok",
            "leaving": False,
            "job_id": "0" * 32,
            "parse_result": {
                "sections": [
                    {"text": "\x01" * per_section, "page": None, "section": None, "sheet": None}
                    for _ in range(MAX_DOCX_BLOCKS)
                ],
                "flags": ["partial_extraction"],
                "page_count": None,
                "sheet_names": None,
                "size_bytes": 20 * 1024 * 1024,
                "needs_vision_handoff": False,
            },
            "page_images": [],
        }
        buffer = io.BytesIO()
        write_frame(buffer, reply)
        assert buffer.tell() < MAX_FRAME_BYTES

    def test_the_worker_refuses_to_send_a_reply_over_the_cap(self, memo: Path) -> None:
        """A reply past the cap is never written; a small typed refusal goes instead."""
        import io  # noqa: PLC0415

        from persona_api.document_worker.protocol import read_frame, write_frame  # noqa: PLC0415

        probe = (
            "import sys\n"
            "from persona_api.document_worker import protocol, worker\n"
            "big = 'a' * (protocol.MAX_FRAME_BYTES + 10)\n"
            "worker._run_job = lambda job: ({'kind': 'ok', 'blob': big}, False)\n"
            "sys.exit(worker.main([]))\n"
        )
        job = io.BytesIO()
        write_frame(job, {"path": str(memo), "render_dir": None, "dpi": 150})
        run = subprocess.run(
            [sys.executable, "-P", "-c", probe],
            input=job.getvalue(),
            capture_output=True,
            check=True,
        )
        frames = io.BytesIO(run.stdout)
        assert read_frame(frames)["kind"] == "ready"  # type: ignore[index]
        refusal = read_frame(frames)
        assert refusal is not None
        assert refusal["error"] == "reply_too_large"
        assert refusal["leaving"] is True
        assert len(run.stdout) < 10_000

    def test_library_output_can_never_corrupt_a_frame(self) -> None:
        """Anything printed, from Python or native code, goes to stderr, not the frames."""
        probe = (
            "import os, sys\n"
            "from persona_api.document_worker.worker import _private_stdout\n"
            "frames = _private_stdout()\n"
            "print('python noise')\n"
            "sys.stdout.flush()\n"
            "os.write(1, b'native noise')\n"
            "frames.write(b'FRAME'); frames.flush()\n"
        )
        run = subprocess.run([sys.executable, "-P", "-c", probe], capture_output=True, check=True)
        assert run.stdout == b"FRAME"
        assert b"python noise" in run.stderr
        assert b"native noise" in run.stderr


class TestAFailedRenderLeavesNothingBehind:
    @pytest.mark.asyncio
    @pytest.mark.usefixtures("fake_worker")
    async def test_pages_written_before_the_worker_died_are_removed(self, tmp_path: Path) -> None:
        render = _pages(tmp_path)
        pool = _pool()
        try:
            with pytest.raises(CorruptDocumentError) as caught:
                await _parse(pool, _mode(tmp_path, "pages_then_die"), render_dir=render)
        finally:
            await pool.aclose()
        assert caught.value.context["limit"] == "crashed"
        assert list(render.iterdir()) == []

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("fake_worker")
    async def test_a_successful_parse_keeps_its_pages(self, tmp_path: Path) -> None:
        """Valid input passes: the render directory is only cleared on failure."""
        render = _pages(tmp_path)
        pool = _pool()
        try:
            parsed = await _parse(pool, _mode(tmp_path, "page:bytes=2048"), render_dir=render)
        finally:
            await pool.aclose()
        assert [p.name for p in parsed.page_images] == ["page-0001.png"]
        assert (render / "page-0001.png").exists()


class TestACompromisedWorkerIsContained:
    """F5 (second review): a stand-in misbehaves the way a taken-over worker could."""

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("fake_worker")
    async def test_the_worker_runs_in_an_empty_directory_of_its_own(self, tmp_path: Path) -> None:
        import json  # noqa: PLC0415

        pool = _pool()
        try:
            parsed = await _parse(pool, _mode(tmp_path, "recon"))
            (pid,) = pool.worker_pids
        finally:
            await pool.aclose()
        info = json.loads(parsed.parse_result.full_text)
        assert info["cwd_entries"] == []
        assert Path(info["cwd"]) != Path.cwd()
        assert Path(info["cwd"]).name.startswith("persona-docworker-")
        assert not Path(info["cwd"]).exists(), "removed with its worker"
        assert pid > 0

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("fake_worker")
    async def test_a_stale_second_reply_is_never_taken_for_the_next_job(
        self, tmp_path: Path
    ) -> None:
        """The reviewer's ``twoframes``: a reply left in the pipe for the next job."""
        pool = _pool()
        try:
            first = await _parse(pool, _mode(tmp_path, "twoframes", "a.pdf"))
            assert first.parse_result.full_text == "reply one, for this job"
            with pytest.raises(CorruptDocumentError) as caught:
                await _parse(pool, _mode(tmp_path, "echo", "b.pdf"))
            assert caught.value.context["limit"] == "protocol"
            assert pool.worker_pids == ()  # killed
            parsed = await _parse(pool, _mode(tmp_path, "echo", "c.pdf"), owner="u2")
            assert parsed.parse_result.full_text == "plain"
        finally:
            await pool.aclose()

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("fake_worker")
    @pytest.mark.parametrize("mode", ["wrongjob", "nojob"])
    async def test_a_reply_for_another_job_is_refused(self, tmp_path: Path, mode: str) -> None:
        pool = _pool()
        try:
            with pytest.raises(CorruptDocumentError) as caught:
                await _parse(pool, _mode(tmp_path, mode))
        finally:
            await pool.aclose()
        assert caught.value.context["limit"] == "protocol"

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("fake_worker")
    async def test_a_worker_never_serves_a_second_owner(self, tmp_path: Path) -> None:
        pool = _pool()
        echo = _mode(tmp_path, "echo")
        try:
            await _parse(pool, echo, owner="u1", purpose="reparse")
            (first,) = pool.worker_pids
            await _parse(pool, echo, owner="u1", purpose="reparse")
            assert pool.worker_pids == (first,)  # valid input passes: same owner, same worker
            await _parse(pool, echo, owner="u2", purpose="reparse")
            (second,) = pool.worker_pids
        finally:
            await pool.aclose()
        assert second != first

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _LINUX, reason="oom_score_adj is Linux only")
    async def test_the_real_worker_is_the_oom_killers_first_choice(self, memo: Path) -> None:
        pool = _pool()
        try:
            await _parse(pool, memo)
            (pid,) = pool.worker_pids
            score = Path(f"/proc/{pid}/oom_score_adj").read_text().strip()
        finally:
            await pool.aclose()
        assert score == "1000"


class TestAnyFailureToReadAReplyKillsTheWorker:
    """F8 (second review): no reply, however malformed, leaves a worker in the slot."""

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("fake_worker")
    async def test_the_reviewers_nested_json_reply_is_a_clean_refusal(self, tmp_path: Path) -> None:
        """200,000 nested arrays exhaust the JSON decoder's stack: RecursionError."""
        pool = _pool()
        try:
            with pytest.raises(CorruptDocumentError) as caught:
                await _parse(pool, _mode(tmp_path, "deepjson"))
            assert pool.worker_pids == ()
            parsed = await _parse(pool, _mode(tmp_path, "echo", "next.pdf"), owner="u2")
            assert parsed.parse_result.full_text == "plain"
        finally:
            await pool.aclose()
        assert caught.value.context["limit"] == "reply"

    def test_read_frame_turns_a_stack_exhausting_body_into_a_frame_error(self) -> None:
        import io  # noqa: PLC0415
        import struct  # noqa: PLC0415

        from persona_api.document_worker.protocol import FrameError, read_frame  # noqa: PLC0415

        body = b'{"x":' + b"[" * 200_000 + b"]" * 200_000 + b"}"
        with pytest.raises(FrameError):
            read_frame(io.BytesIO(struct.pack(">I", len(body)) + body))

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("fake_worker")
    async def test_a_reply_that_is_not_a_valid_result_kills_its_worker(
        self, tmp_path: Path
    ) -> None:
        """A page image outside the render directory: the worker that said so is killed."""
        render = _pages(tmp_path)
        pool = _pool()
        try:
            with pytest.raises(CorruptDocumentError) as caught:
                await _parse(pool, _mode(tmp_path, "page:outside"), render_dir=render)
            assert pool.worker_pids == ()
        finally:
            await pool.aclose()
        assert caught.value.context["limit"] == "reply"

    @pytest.mark.asyncio
    async def test_an_unexpected_error_while_running_a_job_kills_the_worker(
        self, memo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pool = _pool()
        try:
            await _parse(pool, memo)
            (pid,) = pool.worker_pids

            def _boom(*_args: object, **_kwargs: object) -> object:
                raise RecursionError

            monkeypatch.setattr(pool_module, "read_frame", _boom)
            with pytest.raises(CorruptDocumentError):
                await _parse(pool, memo)
            assert pid not in pool.worker_pids
            monkeypatch.undo()
            assert (await _parse(pool, memo, owner="u2")).parse_result.full_text
        finally:
            await pool.aclose()


class TestAnUnexpectedErrorRunningAJob:
    @pytest.mark.asyncio
    async def test_kills_and_clears_the_worker(
        self, memo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """F8: anything the api itself raises mid-job leaves no worker in the slot."""
        pool = _pool()
        try:
            await _parse(pool, memo)
            (pid,) = pool.worker_pids

            def _boom(*_args: object, **_kwargs: object) -> object:
                raise RuntimeError

            monkeypatch.setattr(ProcessDocumentParser, "_exchange", _boom)
            with pytest.raises(CorruptDocumentError) as caught:
                await _parse(pool, memo)
            assert caught.value.context["limit"] == "api_error"
            assert pid not in pool.worker_pids
            monkeypatch.undo()
            assert (await _parse(pool, memo, owner="u2")).parse_result.full_text
        finally:
            await pool.aclose()


class TestRepliesBecomeDomainErrors:
    """A worker's typed error is rebuilt as the same domain error, in the api."""

    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            ("unsupported", UnsupportedFormatError),
            ("corrupt", CorruptDocumentError),
            ("missing_dependency", MissingDependencyError),
        ],
    )
    def test_each_error_kind(self, error: str, expected: type[Exception]) -> None:
        pool = _pool()
        reply = {
            "kind": "error",
            "error": error,
            "message": "m",
            "context": {"format": "docx", "reason": "r"},
        }
        with pytest.raises(expected) as caught:
            pool._parsed(reply, path=Path("upload.docx"))
        assert caught.value.context == {"format": "docx", "reason": "r"}  # type: ignore[attr-defined]

    def test_a_missing_library_in_the_worker_answers_missing_dependency(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The worker's own mapping, run here with the library hidden as in a slim image."""
        from persona_api.document_worker import worker  # noqa: PLC0415

        monkeypatch.setitem(sys.modules, "docx", None)
        source = genuine(tmp_path / "sample.docx", "sample.docx")
        reply, leave = worker._run_job({"path": str(source), "render_dir": None, "dpi": 150})
        assert reply["error"] == "missing_dependency"
        assert reply["context"]["install_hint"] == "pip install persona-core[documents]"
        assert leave is False

    @pytest.mark.parametrize(
        "image",
        [
            "/etc/passwd",
            "{render}/../page-0001.png",
            "{render}/notes.txt",
            "{render}/sub/page-0001.png",
        ],
    )
    def test_a_page_image_outside_the_render_directory_is_refused(
        self, tmp_path: Path, image: str
    ) -> None:
        render = tmp_path / "pages"
        with pytest.raises(CorruptDocumentError):
            ProcessDocumentParser._page_images([image.format(render=render)], render)

    def test_page_images_inside_the_render_directory_pass(self, tmp_path: Path) -> None:
        render = tmp_path / "pages"
        names = [str(render / "page-0001.png"), str(render / "page-0002.png")]
        assert ProcessDocumentParser._page_images(names, render) == tuple(Path(n) for n in names)


class TestTheLimitsAndTheWait:
    """Third review finding 2: the limits, and an upload wait Fly's proxy allows."""

    def test_the_cpu_limit_is_30_s_and_the_deadline_60_s(self) -> None:
        assert pool_module.CPU_SECONDS == 30
        assert pool_module.DEADLINE_SECONDS == 60.0

    def test_the_default_upload_wait_keeps_the_request_under_55_s(self) -> None:
        """Wait, a CPU-bound parse to its limit, and ten seconds for the store."""
        assert pool_module.UPLOAD_WAIT_SECONDS == 15.0
        assert pool_module.UPLOAD_WAIT_SECONDS + pool_module.CPU_SECONDS + 10 <= 55

    def test_the_upload_wait_is_the_default_when_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(pool_module.UPLOAD_WAIT_ENV_VAR, raising=False)
        assert pool_module.upload_wait_from_env() == 15.0

    @pytest.mark.parametrize("raw", ["1", "40", "300", "2.5"])
    def test_the_upload_wait_is_settable(self, raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(pool_module.UPLOAD_WAIT_ENV_VAR, raw)
        assert pool_module.upload_wait_from_env() == float(raw)

    @pytest.mark.parametrize("raw", ["0", "0.5", "301", "nan", "inf", "soon", "-5"])
    def test_an_unusable_upload_wait_falls_back_to_the_default(
        self, raw: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(pool_module.UPLOAD_WAIT_ENV_VAR, raw)
        assert pool_module.upload_wait_from_env() == 15.0

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("fake_worker")
    async def test_the_pool_waits_as_long_as_the_setting_says(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(pool_module.UPLOAD_WAIT_ENV_VAR, "1")
        pool = _pool()
        try:
            busy = asyncio.create_task(
                _parse(pool, _mode(tmp_path, "slow:2.5", "a.pdf"), owner="u1")
            )
            await asyncio.sleep(0.3)
            started = asyncio.get_running_loop().time()
            with pytest.raises(DocumentParserBusyError):
                await _parse(pool, _mode(tmp_path, "echo", "b.pdf"), owner="u2")
            waited = asyncio.get_running_loop().time() - started
            await busy
        finally:
            await pool.aclose()
        assert 0.9 <= waited < 2.0


class TestChargedByCpu:
    """Each job is charged the CPU its worker used, not the time it waited for a CPU."""

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _LINUX, reason="/proc gives the CPU seconds on Linux only")
    @pytest.mark.usefixtures("fake_worker")
    async def test_a_job_that_waits_is_charged_its_cpu_not_its_wall_time(
        self, tmp_path: Path
    ) -> None:
        pool = _pool()
        try:
            await _parse(pool, _mode(tmp_path, "slow:1.0"), owner="u1")
        finally:
            await pool.aclose()
        assert pool.admission.worker_seconds_used("u1") < 0.3

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("fake_worker")
    async def test_without_proc_the_wall_seconds_are_charged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """macOS and Windows: no per-process CPU to read, so the job's wall time."""
        monkeypatch.setattr(pool_module, "_process_cpu_seconds", lambda _pid: None)
        pool = _pool()
        try:
            await _parse(pool, _mode(tmp_path, "slow:0.5"), owner="u1")
        finally:
            await pool.aclose()
        assert 0.5 <= pool.admission.worker_seconds_used("u1") < 5

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("fake_worker")
    async def test_a_job_killed_at_the_deadline_is_charged_its_wall_seconds(
        self, tmp_path: Path
    ) -> None:
        """It used no CPU, but it held the worker until the deadline."""
        pool = _pool(deadline_seconds=1)
        try:
            with pytest.raises(CorruptDocumentError):
                await _parse(pool, _mode(tmp_path, "sleep"), owner="u1")
        finally:
            await pool.aclose()
        assert 1.0 <= pool.admission.worker_seconds_used("u1") < 5

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _LINUX, reason="RLIMIT_CPU and /proc: Linux")
    async def test_a_job_killed_by_the_cpu_limit_is_charged_the_cpu_it_used(
        self, bomb: Path
    ) -> None:
        """Read from the killed worker before it is reaped."""
        pool = _pool(cpu_seconds=1, deadline_seconds=60)
        try:
            with pytest.raises(CorruptDocumentError) as caught:
                await _parse(pool, bomb, owner="u1")
        finally:
            await pool.aclose()
        assert caught.value.context["limit"] == "cpu_limit"
        assert 0.9 <= pool.admission.worker_seconds_used("u1") < 5

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _LINUX, reason="/proc gives the CPU seconds on Linux only")
    @pytest.mark.usefixtures("fake_worker")
    async def test_a_crashed_worker_is_charged_its_cpu_read_before_it_was_reaped(
        self, tmp_path: Path
    ) -> None:
        """0.2 s of CPU, then 1.5 s of waiting, then a crash: charged the 0.2 s."""
        pool = _pool()
        try:
            with pytest.raises(CorruptDocumentError) as caught:
                await _parse(pool, _mode(tmp_path, "spin_then_die:0.2:1.5"), owner="u1")
        finally:
            await pool.aclose()
        assert caught.value.context["limit"] == "crashed"
        assert 0.15 <= pool.admission.worker_seconds_used("u1") < 1.0

    @pytest.mark.skipif(not _LINUX, reason="/proc: Linux")
    def test_the_cpu_seconds_of_a_process_are_read_from_proc(self) -> None:
        spin = subprocess.Popen(  # noqa: S603 (fixed argv)
            [
                sys.executable,
                "-c",
                "import time, sys\n"
                "end = time.process_time() + 0.5\n"
                "while time.process_time() < end: pass\n"
                "sys.stdout.write('x'); sys.stdout.flush(); time.sleep(30)",
            ],
            stdout=subprocess.PIPE,
        )
        try:
            assert spin.stdout is not None
            spin.stdout.read(1)
            used = pool_module._process_cpu_seconds(spin.pid)
        finally:
            spin.kill()
            spin.wait()
        assert used is not None
        assert 0.45 <= used < 3
        assert pool_module._process_cpu_seconds(spin.pid) is None  # reaped: gone


class TestOnlyRealLimitsCoolAnOwnerDown:
    """Third review finding 7: no cooldown for what is not the file's doing."""

    @pytest.mark.parametrize(
        ("outcome", "expected"),
        [
            ({"kind": "failed", "failure": "deadline"}, True),
            ({"kind": "failed", "failure": "cpu_limit"}, True),
            ({"kind": "failed", "failure": "crashed"}, True),
            ({"kind": "failed", "failure": "signal_9"}, True),
            ({"kind": "failed", "failure": "reply"}, True),
            ({"kind": "failed", "failure": "protocol"}, True),
            ({"kind": "failed", "failure": "api_error"}, False),
            ({"kind": "error", "error": "memory_limit"}, True),
            ({"kind": "error", "error": "reply_too_large"}, True),
            ({"kind": "error", "error": "internal"}, False),
            ({"kind": "error", "error": "corrupt"}, False),
            ({"kind": "ok"}, False),
        ],
    )
    def test_which_outcomes_are_limits(self, outcome: dict[str, str], expected: bool) -> None:
        assert pool_module._hit_limit(outcome) is expected

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("fake_worker")
    async def test_an_internal_error_in_the_worker_does_not_cool_its_owner_down(
        self, tmp_path: Path
    ) -> None:
        pool = _pool()
        try:
            with pytest.raises(CorruptDocumentError):
                await _parse(pool, _mode(tmp_path, "internal"), owner="u1")
            assert not pool.admission.is_suspect("u1")
            assert (await _parse(pool, _mode(tmp_path, "echo", "e.pdf"), owner="u1")).parse_result
        finally:
            await pool.aclose()

    @pytest.mark.asyncio
    async def test_a_failure_in_the_api_does_not_cool_its_owner_down(
        self, memo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pool = _pool()

        def _boom(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError

        try:
            monkeypatch.setattr(ProcessDocumentParser, "_exchange", _boom)
            with pytest.raises(CorruptDocumentError):
                await _parse(pool, memo, owner="u1")
            monkeypatch.undo()
            assert (await _parse(pool, memo, owner="u1")).parse_result.full_text
        finally:
            await pool.aclose()


class TestSuspectOwnersGoLast:
    """Third review finding 2: an owner whose file hit a limit waits behind the others."""

    @staticmethod
    def _suspect_pool(owner: str) -> ProcessDocumentParser:
        from persona_api.document_worker.admission import DocumentAdmission  # noqa: PLC0415

        admission = DocumentAdmission(cooldown_seconds=0)
        admission.charge(owner, 0.0, hit_limit=True)
        assert admission.is_suspect(owner), "precondition"
        return _pool(admission=admission, queue_depth=10, reparse_wait_seconds=30)

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("fake_worker")
    async def test_a_suspect_owner_queued_first_is_served_after_another_owner(
        self, tmp_path: Path
    ) -> None:
        pool = self._suspect_pool("bad")
        finished: list[str] = []

        async def job(name: str, owner: str, mode: str) -> None:
            await _parse(pool, _mode(tmp_path, mode, f"{name}.pdf"), owner=owner, purpose="reparse")
            finished.append(name)

        try:
            tasks = [asyncio.create_task(job("busy", "u3", "slow:0.4"))]
            await asyncio.sleep(0.2)
            tasks.append(asyncio.create_task(job("suspect", "bad", "echo")))
            await asyncio.sleep(0.01)
            tasks.append(asyncio.create_task(job("honest", "u2", "echo")))
            await asyncio.gather(*tasks)
        finally:
            await pool.aclose()
        assert finished == ["busy", "honest", "suspect"]

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("fake_worker")
    async def test_a_suspect_owner_is_served_when_no_one_else_waits(self, tmp_path: Path) -> None:
        pool = self._suspect_pool("bad")
        finished: list[str] = []

        async def job(name: str, owner: str, mode: str) -> None:
            await _parse(pool, _mode(tmp_path, mode, f"{name}.pdf"), owner=owner, purpose="reparse")
            finished.append(name)

        try:
            tasks = [asyncio.create_task(job("busy", "u3", "slow:0.4"))]
            await asyncio.sleep(0.2)
            tasks.append(asyncio.create_task(job("suspect", "bad", "echo")))
            await asyncio.gather(*tasks)
            await job("again", "bad", "echo")
        finally:
            await pool.aclose()
        assert finished == ["busy", "suspect", "again"]

    def test_an_owner_is_suspect_for_ten_minutes_after_a_limit(self) -> None:
        from persona_api.document_worker import admission as admission_module  # noqa: PLC0415

        now = [1000.0]
        admission = admission_module.DocumentAdmission(clock=lambda: now[0])
        assert admission_module.SUSPECT_SECONDS == 600.0
        admission.charge("u1", 1.0, hit_limit=False)
        assert not admission.is_suspect("u1")
        admission.charge("u1", 1.0, hit_limit=True)
        now[0] += 599
        assert admission.is_suspect("u1")
        assert not admission.is_suspect("u2")
        now[0] += 1
        assert not admission.is_suspect("u1")


def _spare_settled(pool: ProcessDocumentParser) -> None:
    """Wait for the spare's start to finish: its thread runs one task at a time."""
    spare_thread = pool._spare_thread
    assert spare_thread is not None, "precondition: a spare was asked for"
    spare_thread.submit(lambda: None).result(timeout=30)


class TestTheSpareWorker:
    """Third review finding 13: a new owner takes a pre-started spare, bound to them alone."""

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("fake_worker")
    async def test_a_spare_starts_with_no_owner_and_serves_only_the_owner_who_takes_it(
        self, tmp_path: Path
    ) -> None:
        pool = _pool()
        echo = _mode(tmp_path, "echo")
        served: dict[int, set[str]] = {}

        async def parse_as(owner: str) -> int:
            await _parse(pool, echo, owner=owner, purpose="reparse")
            (pid,) = pool.worker_pids
            served.setdefault(pid, set()).add(owner)
            return pid

        try:
            first = await parse_as("u1")
            await asyncio.to_thread(_spare_settled, pool)
            spare = pool.spare_pid
            assert spare is not None
            assert spare != first
            assert pool._spare.worker is not None
            assert pool._spare.worker.owner_id is None  # pre-started with no owner
            assert await parse_as("u2") == spare  # the new owner takes the spare
            bound = pool._slots[0].worker
            assert bound is not None
            assert bound.owner_id == "u2"  # bound to the first owner to take it
            await asyncio.to_thread(_spare_settled, pool)
            second_spare = pool.spare_pid
            assert second_spare not in (None, first, spare)
            assert await parse_as("u2") == spare  # its owner keeps it
            back = await parse_as("u1")  # u1 again: never u2's worker
            assert back == second_spare
            await asyncio.to_thread(_spare_settled, pool)
            assert await parse_as("u2") not in (spare, second_spare)
        finally:
            await pool.aclose()
        assert all(len(owners) == 1 for owners in served.values()), served

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("fake_worker")
    async def test_a_dead_spare_is_never_used(self, tmp_path: Path) -> None:
        pool = _pool()
        echo = _mode(tmp_path, "echo")
        try:
            await _parse(pool, echo, owner="u1", purpose="reparse")
            await asyncio.to_thread(_spare_settled, pool)
            spare = pool.spare_pid
            assert spare is not None
            os.kill(spare, 9)
            assert pool._spare.worker is not None
            pool._spare.worker.process.wait(timeout=10)
            assert (await _parse(pool, echo, owner="u2", purpose="reparse")).parse_result
            (pid,) = pool.worker_pids
        finally:
            await pool.aclose()
        assert pid != spare

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("fake_worker")
    async def test_the_pool_can_run_without_a_spare(self, tmp_path: Path) -> None:
        pool = _pool(spare_worker=False)
        try:
            await _parse(pool, _mode(tmp_path, "echo"), owner="u1", purpose="reparse")
            assert pool._spare_thread is None
            assert pool.spare_pid is None
        finally:
            await pool.aclose()

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("fake_worker")
    async def test_closing_the_pool_kills_the_spare(self, tmp_path: Path) -> None:
        pool = _pool()
        await _parse(pool, _mode(tmp_path, "echo"), owner="u1", purpose="reparse")
        await asyncio.to_thread(_spare_settled, pool)
        spare = pool.spare_pid
        assert spare is not None
        await pool.aclose()
        assert pool.spare_pid is None
        if _POSIX:
            with pytest.raises(ProcessLookupError):
                os.kill(spare, 0)


class TestTheApiBoundsTheReply:
    """Third review finding 11: the api re-applies the document bounds to any reply."""

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("fake_worker")
    async def test_text_past_the_budget_is_cut_in_the_api(self, tmp_path: Path) -> None:
        from persona.documents.parsers import (  # noqa: PLC0415
            FLAG_PARTIAL_EXTRACTION,
            MAX_DOCUMENT_TEXT_CHARS,
        )

        pool = _pool()
        try:
            parsed = await _parse(pool, _mode(tmp_path, f"huge:{MAX_DOCUMENT_TEXT_CHARS + 100}"))
        finally:
            await pool.aclose()
        assert len(parsed.parse_result.full_text) == MAX_DOCUMENT_TEXT_CHARS
        assert FLAG_PARTIAL_EXTRACTION in parsed.parse_result.flags

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("fake_worker")
    async def test_text_within_the_budget_is_untouched(self, tmp_path: Path) -> None:
        from persona.documents.parsers import MAX_DOCUMENT_TEXT_CHARS  # noqa: PLC0415

        pool = _pool()
        try:
            parsed = await _parse(pool, _mode(tmp_path, f"huge:{MAX_DOCUMENT_TEXT_CHARS}"))
        finally:
            await pool.aclose()
        assert len(parsed.parse_result.full_text) == MAX_DOCUMENT_TEXT_CHARS
        assert parsed.parse_result.flags == ()

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("fake_worker")
    @pytest.mark.parametrize("field", ["sections", "sheets"])
    async def test_more_sections_than_a_genuine_worker_sends_kills_the_worker(
        self, tmp_path: Path, field: str
    ) -> None:
        from persona.documents.parsers import MAX_DOCUMENT_SECTIONS  # noqa: PLC0415

        pool = _pool()
        try:
            with pytest.raises(CorruptDocumentError) as caught:
                await _parse(pool, _mode(tmp_path, f"{field}:{MAX_DOCUMENT_SECTIONS + 1}"))
            assert pool.worker_pids == ()
        finally:
            await pool.aclose()
        assert caught.value.context["limit"] == "reply"

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("fake_worker")
    @pytest.mark.parametrize("field", ["sections", "sheets"])
    async def test_sections_at_the_cap_pass(self, tmp_path: Path, field: str) -> None:
        from persona.documents.parsers import MAX_DOCUMENT_SECTIONS  # noqa: PLC0415

        pool = _pool()
        try:
            parsed = await _parse(pool, _mode(tmp_path, f"{field}:{MAX_DOCUMENT_SECTIONS}"))
        finally:
            await pool.aclose()
        result = parsed.parse_result
        count = len(result.sections) if field == "sections" else len(result.sheet_names or ())
        assert count == MAX_DOCUMENT_SECTIONS
