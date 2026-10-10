"""T17 — content-type dispatch tests for ``POST /v1/personas/:id/uploads``.

Verifies the CSA-2 dispatcher in ``routes/uploads.py``: ``image/*``
content-type routes to the Spec 13 image_service path (covered by
``test_uploads.py``); document MIME types + supported extensions route
to Spec 14's document_service path; anything else returns 415.

No DB — mocks ``chat_service.get_conversation`` +
``persona_service.get_persona`` to return synthetic rows; uses an
in-memory ``DocumentStore`` and a ``tmp_path``-backed workspace root.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

import pytest
from fastapi.testclient import TestClient
from persona.stores.document_store import DocumentStore
from persona_api.app import create_app
from persona_api.auth import AuthenticatedUser
from persona_api.config import APIConfig
from persona_api.document_worker.pool import ProcessDocumentParser
from persona_api.errors import ConversationNotFoundError, PersonaNotFoundError
from persona_api.services import audit_service, chat_service, persona_service
from persona_api.services.document_parsing import ThreadDocumentParser

if TYPE_CHECKING:
    from persona.schema.chunks import PersonaChunk


class _InMemoryBackend:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], list[PersonaChunk]] = {}

    def upsert(
        self,
        *,
        persona_id: str,
        store_kind: str,
        chunks: list[PersonaChunk],
    ) -> None:
        key = (persona_id, store_kind)
        existing = self.store.setdefault(key, [])
        existing_ids = {c.id for c in chunks}
        kept = [c for c in existing if c.id not in existing_ids]
        kept.extend(chunks)
        self.store[key] = kept

    def query(
        self,
        *,
        persona_id: str,
        store_kind: str,
        text: str,  # noqa: ARG002
        top_k: int,
        where: dict[str, Any] | None = None,  # noqa: ARG002
    ) -> list[PersonaChunk]:
        return list(self.store.get((persona_id, store_kind), []))[:top_k]

    def get_all(self, *, persona_id: str, store_kind: str) -> list[PersonaChunk]:
        return list(self.store.get((persona_id, store_kind), []))

    def delete_persona(self, persona_id: str, store_kind: str) -> None:
        self.store.pop((persona_id, store_kind), None)

    def delete_documents(self, *, persona_id: str, store_kind: str, ids: list[str]) -> None:
        key = (persona_id, store_kind)
        self.store[key] = [c for c in self.store.get(key, []) if c.id not in set(ids)]


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


@pytest.fixture
def document_store() -> DocumentStore:
    return DocumentStore(backend=_InMemoryBackend())  # type: ignore[arg-type]


@pytest.fixture
def client(
    monkeypatch: pytest.MonkeyPatch,
    workspace_root: Path,
    document_store: DocumentStore,
) -> Iterator[TestClient]:
    """The app as built, so uploads parse in the real worker pool (D-FV0-31)."""
    app = create_app(
        # Cloud auth wall, but no lifespan engine is built here (the fixture
        # returns the client without entering its context + sets rls_engine=None).
        # Distinct app DSN satisfies the R2 cloud-config guard (R2-D-1).
        APIConfig(
            database_url="postgresql+psycopg://super@localhost/persona_shell",
            app_database_url="postgresql+psycopg://persona_app@localhost/persona_shell",
        )
    )

    async def _verify(token: str) -> AuthenticatedUser:
        return AuthenticatedUser(id=token, email=None)

    from persona_api.middleware.rate_limit import InMemoryRateLimitStore, RateLimiter

    app.state.verify_token = _verify
    app.state.rls_engine = None
    app.state.workspace_root = workspace_root
    app.state.build_document_store = lambda: document_store
    # Rate limiter (in-memory; never hits the limit in these tests).
    app.state.rate_limiter = RateLimiter(
        InMemoryRateLimitStore(), default_limit=1000, per_endpoint={}
    )

    # Mock persona pre-flight (Spec 13's _ensure_persona_visible path).
    def _fake_get_persona(*, rls_engine: Any, persona_id: str) -> dict[str, Any]:  # noqa: ANN401, ARG001
        if persona_id in ("astrid", "other_persona"):
            return {"id": persona_id, "owner_id": "u1", "yaml": ""}
        raise PersonaNotFoundError("persona not found", context={"persona_id": persona_id})

    monkeypatch.setattr(persona_service, "get_persona", _fake_get_persona)

    # Mock conversation lookup for document path.
    def _fake_get_conversation(*, rls_engine: Any, conversation_id: str) -> dict[str, Any]:  # noqa: ANN401, ARG001
        if conversation_id == "conv_u1":
            return {
                "id": "conv_u1",
                "owner_id": "u1",
                "persona_id": "astrid",
                "title": "test",
                "messages": [],
                "created_at": None,
                "updated_at": None,
            }
        raise ConversationNotFoundError(
            "conversation not found", context={"conversation_id": conversation_id}
        )

    monkeypatch.setattr(chat_service, "get_conversation", _fake_get_conversation)
    monkeypatch.setattr(audit_service, "record", lambda **_kw: None)

    yield TestClient(app)
    parser = app.state.document_parser
    if isinstance(parser, ProcessDocumentParser):
        asyncio.run(parser.aclose())


@pytest.fixture
def in_process_parser(client: TestClient) -> None:
    """Parse in this process, for tests that replace a parse function here.

    A replacement made in the test process cannot reach a worker process, so these
    tests swap in the in-process port. They test the route around the parse; the pool
    has its own tests, which drive real workers.
    """
    client.app.state.document_parser = ThreadDocumentParser()  # type: ignore[attr-defined]


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer u1"}


class TestDocumentMIMEDispatch:
    """Documents (PDF / docx / xlsx / csv / txt / md / code) dispatch to
    document_service via the content-type / filename detector."""

    def test_txt_upload_routes_to_document_service(self, client: TestClient) -> None:
        resp = client.post(
            "/v1/personas/astrid/uploads",
            headers=_auth(),
            files={
                "file": ("memo.txt", b"The lease runs for twelve months.", "text/plain"),
            },
            data={"conversation_id": "conv_u1"},
        )
        assert resp.status_code == 201
        payload = resp.json()
        # The DocumentRef shape is the document path's return type.
        assert "doc_ref" in payload
        assert payload["format"] == "txt"
        assert payload["strategy"] == "whole_inject"
        assert payload["token_count"] > 0

    def test_csv_upload_routes_to_document_service(self, client: TestClient) -> None:
        resp = client.post(
            "/v1/personas/astrid/uploads",
            headers=_auth(),
            files={
                "file": ("data.csv", b"a,b\n1,2\n3,4\n", "text/csv"),
            },
            data={"conversation_id": "conv_u1"},
        )
        assert resp.status_code == 201
        assert resp.json()["format"] == "csv"

    def test_markdown_upload_routes_to_document_service(self, client: TestClient) -> None:
        resp = client.post(
            "/v1/personas/astrid/uploads",
            headers=_auth(),
            files={
                "file": ("notes.md", b"# Heading\n\nBody.", "text/markdown"),
            },
            data={"conversation_id": "conv_u1"},
        )
        assert resp.status_code == 201
        assert resp.json()["format"] == "md"

    def test_pdf_content_type_routes_to_document_service(self, client: TestClient) -> None:
        # A real text-PDF fixture from the core test fixtures.
        pdf_path = (
            Path(__file__).resolve().parents[2]
            / "core"
            / "tests"
            / "fixtures"
            / "documents"
            / "sample-text.pdf"
        ).resolve()
        pdf_bytes = pdf_path.read_bytes()
        resp = client.post(
            "/v1/personas/astrid/uploads",
            headers=_auth(),
            files={"file": ("tenancy.pdf", pdf_bytes, "application/pdf")},
            data={"conversation_id": "conv_u1"},
        )
        assert resp.status_code == 201
        assert resp.json()["format"] == "pdf"


class TestConversationIdRequired:
    """Documents are conversation-scoped — uploads must carry conversation_id."""

    def test_document_without_conversation_id_returns_422(self, client: TestClient) -> None:
        resp = client.post(
            "/v1/personas/astrid/uploads",
            headers=_auth(),
            files={"file": ("memo.txt", b"hi", "text/plain")},
            # NO conversation_id form field
        )
        assert resp.status_code == 422
        body = resp.json()
        assert body["detail"]["error"] == "conversation_id_required"

    def test_document_with_empty_conversation_id_returns_422(self, client: TestClient) -> None:
        resp = client.post(
            "/v1/personas/astrid/uploads",
            headers=_auth(),
            files={"file": ("memo.txt", b"hi", "text/plain")},
            data={"conversation_id": "   "},
        )
        assert resp.status_code == 422
        assert resp.json()["detail"]["error"] == "conversation_id_required"


class TestUnsupportedMediaType:
    def test_unknown_extension_returns_415(self, client: TestClient) -> None:
        resp = client.post(
            "/v1/personas/astrid/uploads",
            headers=_auth(),
            files={
                "file": ("archive.rar", b"junk-bytes", "application/x-rar"),
            },
            data={"conversation_id": "conv_u1"},
        )
        assert resp.status_code == 415

    def test_no_extension_no_content_type_returns_415(self, client: TestClient) -> None:
        resp = client.post(
            "/v1/personas/astrid/uploads",
            headers=_auth(),
            files={
                "file": ("noext", b"some bytes", "application/octet-stream"),
            },
            data={"conversation_id": "conv_u1"},
        )
        assert resp.status_code == 415


class TestCrossTenantConversation:
    def test_unknown_conversation_returns_404(self, client: TestClient) -> None:
        resp = client.post(
            "/v1/personas/astrid/uploads",
            headers=_auth(),
            files={"file": ("memo.txt", b"hi", "text/plain")},
            data={"conversation_id": "conv_OTHER_TENANT"},
        )
        assert resp.status_code == 404


class TestCorruptDocument:
    def test_empty_bytes_returns_422_corrupt(self, client: TestClient) -> None:
        resp = client.post(
            "/v1/personas/astrid/uploads",
            headers=_auth(),
            files={
                "file": ("empty.txt", b"   \n\n  \n", "text/plain"),
            },
            data={"conversation_id": "conv_u1"},
        )
        assert resp.status_code == 422
        body = resp.json()
        assert body["detail"]["error"] == "corrupt_document"


class TestVisionHandoffPath:
    """T21 — scanned PDFs succeed via rasterisation + ImageContent
    (no longer 422). The interim VisionHandoffRequiredError → 422 path has
    been removed per the TODO(T21) close-out.
    """

    def test_scanned_pdf_returns_201_with_vision_handoff_strategy(self, client: TestClient) -> None:
        scanned_path = (
            Path(__file__).resolve().parents[2]
            / "core"
            / "tests"
            / "fixtures"
            / "documents"
            / "scanned-like.pdf"
        ).resolve()
        scanned_bytes = scanned_path.read_bytes()
        resp = client.post(
            "/v1/personas/astrid/uploads",
            headers=_auth(),
            files={"file": ("scan.pdf", scanned_bytes, "application/pdf")},
            data={"conversation_id": "conv_u1"},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["strategy"] == "vision_handoff"
        assert len(body["images"]) == 3  # fixture has 3 pages
        for image in body["images"]:
            assert image["type"] == "image"
            assert image["media_type"] == "image/png"
            assert image["workspace_path"].endswith(".png")


class TestAuthRequired:
    def test_unauthenticated_returns_401(self, client: TestClient) -> None:
        resp = client.post(
            "/v1/personas/astrid/uploads",
            files={"file": ("memo.txt", b"hi", "text/plain")},
            data={"conversation_id": "conv_u1"},
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# D-FV0-08 / D-FV0-09 (T0): the document parsers reach production, so the upload path
# gets a byte cap, a bounded read, the archive guard, and an honest 503 when a parser
# library is missing.
# ---------------------------------------------------------------------------


def _office_bytes(kind: str) -> bytes:
    import io  # noqa: PLC0415

    buffer = io.BytesIO()
    if kind == "docx":
        import docx  # noqa: PLC0415

        document = docx.Document()
        document.add_paragraph("The lease runs for twelve months.")
        document.save(buffer)
    else:
        import openpyxl  # noqa: PLC0415

        workbook = openpyxl.Workbook()
        workbook.active["A1"] = "rent"  # type: ignore[index]
        workbook.save(buffer)
    return buffer.getvalue()


def _isolated_storage(client: TestClient, root: Path) -> Path:
    """Point the app's storage at ``root`` so a test can prove nothing was written."""
    from persona_api.storage import LocalFileStorage  # noqa: PLC0415

    root.mkdir(parents=True, exist_ok=True)
    client.app.state.file_storage = LocalFileStorage(root)  # type: ignore[attr-defined]
    return root


def _stored_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file()]


class TestDocumentByteCap:
    """The cap is crossed in bytes: exactly 20 MiB reaches the parser, one more does not."""

    @pytest.fixture
    def parse_calls(
        self,
        monkeypatch: pytest.MonkeyPatch,
        in_process_parser: None,  # noqa: ARG002 (the recorder lives in this process)
    ) -> list[int]:
        """Replace the parse with a recorder that refuses, so a 20 MiB body is cheap.

        The recorder proves the upload got PAST the cap (it was called) without paying
        for tokenising twenty megabytes of text.
        """
        from persona.documents.errors import CorruptDocumentError  # noqa: PLC0415
        from persona_api.document_worker import untrusted as document_parsing  # noqa: PLC0415

        calls: list[int] = []

        def _recording_parse(path: Path) -> object:
            calls.append(path.stat().st_size)
            raise CorruptDocumentError(
                "stub parse", context={"format": "txt", "reason": "stub", "filename": path.name}
            )

        monkeypatch.setattr(document_parsing, "parse_document", _recording_parse)
        return calls

    def test_the_caps_on_every_upload_branch_are_one_number(self) -> None:
        """The route's bounded read assumes no branch allows more than the read takes."""
        from persona_api.routes import uploads  # noqa: PLC0415
        from persona_api.services import (  # noqa: PLC0415
            document_service,
            image_service,
            task_attachment_service,
        )

        cap = image_service.MAX_UPLOAD_BYTES
        assert cap == 20 * 1024 * 1024
        assert cap == document_service.MAX_DOCUMENT_BYTES
        assert cap == task_attachment_service.MAX_ATTACHMENT_BYTES
        assert cap + 1 == uploads._MAX_UPLOAD_READ_BYTES

    def test_a_document_of_exactly_the_cap_reaches_the_parser(
        self, client: TestClient, parse_calls: list[int]
    ) -> None:
        from persona_api.services.document_service import MAX_DOCUMENT_BYTES  # noqa: PLC0415

        body = b"a" * MAX_DOCUMENT_BYTES
        resp = client.post(
            "/v1/personas/astrid/uploads",
            headers=_auth(),
            files={"file": ("big.txt", body, "text/plain")},
            data={"conversation_id": "conv_u1"},
        )
        assert parse_calls == [MAX_DOCUMENT_BYTES]
        assert resp.status_code == 422
        assert resp.json()["detail"]["error"] == "corrupt_document"

    def test_a_document_one_byte_over_the_cap_is_refused_before_parsing(
        self, client: TestClient, parse_calls: list[int], tmp_path: Path
    ) -> None:
        from persona_api.services.document_service import MAX_DOCUMENT_BYTES  # noqa: PLC0415

        root = _isolated_storage(client, tmp_path / "store")
        resp = client.post(
            "/v1/personas/astrid/uploads",
            headers=_auth(),
            files={"file": ("big.txt", b"a" * (MAX_DOCUMENT_BYTES + 1), "text/plain")},
            data={"conversation_id": "conv_u1"},
        )
        assert resp.status_code == 413
        detail = resp.json()["detail"]
        assert detail["error"] == "upload_too_large"
        assert detail["context"]["max_bytes"] == str(MAX_DOCUMENT_BYTES)
        assert detail["context"]["size_bytes"] == str(MAX_DOCUMENT_BYTES + 1)
        assert parse_calls == []
        assert _stored_files(root) == []

    def test_the_route_reads_at_most_one_byte_past_the_cap(
        self, client: TestClient, parse_calls: list[int]
    ) -> None:
        """A body far over the cap is never pulled into memory whole.

        The service reports the size it was handed; it sees cap + 1 bytes, not the
        five megabytes over the cap that were sent.
        """
        from persona_api.services.document_service import MAX_DOCUMENT_BYTES  # noqa: PLC0415

        # Inside the request-body limit (which has 64 KiB of multipart headroom), so the
        # body is accepted and it is the route's bounded read that is being proven.
        sent = MAX_DOCUMENT_BYTES + 32 * 1024
        resp = client.post(
            "/v1/personas/astrid/uploads",
            headers=_auth(),
            files={"file": ("big.txt", b"a" * sent, "text/plain")},
            data={"conversation_id": "conv_u1"},
        )
        assert resp.status_code == 413
        assert resp.json()["detail"]["context"]["size_bytes"] == str(MAX_DOCUMENT_BYTES + 1)
        assert parse_calls == []


class TestOneAnswerForTooLarge:
    """PR 17: every upload branch answers a file too large with 413 ``upload_too_large``.

    Whether the body bound found it (over 20 MiB + 64 KiB) or the branch's own cap did (a
    file over 20 MiB inside the body bound): an image, a document and a task attachment.
    """

    _BRANCHES: ClassVar[dict[str, tuple[str, str, dict[str, str]]]] = {
        "image": ("big.png", "image/png", {}),
        "document": ("big.txt", "text/plain", {"conversation_id": "conv_u1"}),
        "task attachment": ("big.txt", "text/plain", {"scope": "task"}),
    }

    @pytest.mark.parametrize("branch", ["image", "document", "task attachment"])
    @pytest.mark.parametrize("over", ["the file cap", "the body bound"])
    def test_it_is_413_upload_too_large(
        self, client: TestClient, tmp_path: Path, branch: str, over: str
    ) -> None:
        from persona_api.routes import uploads  # noqa: PLC0415
        from persona_api.services import image_service  # noqa: PLC0415

        _isolated_storage(client, tmp_path / "store")
        name, media_type, data = self._BRANCHES[branch]
        size = (
            image_service.MAX_UPLOAD_BYTES + 1
            if over == "the file cap"
            else uploads.MAX_UPLOAD_BODY_BYTES + 1
        )
        resp = client.post(
            "/v1/personas/astrid/uploads",
            headers=_auth(),
            files={"file": (name, b"\x89PNG\r\n\x1a\n" + b"\x00" * (size - 8), media_type)},
            data=data,
        )
        assert resp.status_code == 413, resp.text
        detail = resp.json()["detail"]
        assert detail["error"] == "upload_too_large"
        assert detail["detail"] == "The file is too large: the upload limit is 20 MB."
        assert detail["context"]["reason"] == "oversize"
        assert detail["context"]["max_bytes"] == str(image_service.MAX_UPLOAD_BYTES)


class TestUploadRequestBodyLimit:
    """The request body is bounded before Starlette spools any of it (review item 6)."""

    def test_the_body_limit_is_the_upload_cap_plus_multipart_headroom(self) -> None:
        from persona_api.routes import uploads  # noqa: PLC0415
        from persona_api.services import image_service  # noqa: PLC0415

        assert uploads.MAX_UPLOAD_BODY_BYTES == image_service.MAX_UPLOAD_BYTES + 64 * 1024

    def test_a_declared_length_over_the_limit_is_refused_before_reading(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from persona_api.routes import uploads  # noqa: PLC0415

        read: list[int] = []
        real = uploads._counting_receive

        def _spy(receive: Any) -> Any:  # noqa: ANN401
            # Record at the RAW receive, before the counter can raise: any body read at
            # all means the declared length was not refused up front.
            async def raw() -> Any:  # noqa: ANN401
                message = await receive()
                read.append(len(message.get("body", b"")))
                return message

            return real(raw)

        monkeypatch.setattr(uploads, "_counting_receive", _spy)
        body = b"a" * (uploads.MAX_UPLOAD_BODY_BYTES + 1)
        resp = client.post(
            "/v1/personas/astrid/uploads",
            headers=_auth(),
            files={"file": ("big.txt", body, "text/plain")},
            data={"conversation_id": "conv_u1"},
        )
        assert resp.status_code == 413
        detail = resp.json()["detail"]
        assert detail["error"] == "upload_too_large"
        assert int(detail["context"]["received_bytes"]) > uploads.MAX_UPLOAD_BODY_BYTES
        assert read == [], "the body was read although its declared length was refused"

    def test_a_body_at_the_limit_is_accepted_by_the_route(self, client: TestClient) -> None:
        """Valid input passes: a body exactly at the limit reaches the handler."""
        from persona_api.routes import uploads  # noqa: PLC0415

        boundary = "b0undary"
        head = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="conversation_id"\r\n\r\n'
            f'conv_u1\r\n--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            f'filename="memo.txt"\r\nContent-Type: text/plain\r\n\r\n'
        ).encode()
        tail = f"\r\n--{boundary}--\r\n".encode()
        filler = uploads.MAX_UPLOAD_BODY_BYTES - len(head) - len(tail)
        body = head + b"a" * filler + tail
        assert len(body) == uploads.MAX_UPLOAD_BODY_BYTES
        resp = client.post(
            "/v1/personas/astrid/uploads",
            headers={**_auth(), "content-type": f"multipart/form-data; boundary={boundary}"},
            content=body,
        )
        # Over the 20 MiB document cap, so the service refuses it: but the ROUTE took it.
        assert resp.status_code == 413
        assert resp.json()["detail"]["error"] == "upload_too_large"

    def test_a_second_file_part_is_refused(self, client: TestClient) -> None:
        resp = client.post(
            "/v1/personas/astrid/uploads",
            headers=_auth(),
            files=[
                ("file", ("a.txt", b"one", "text/plain")),
                ("file", ("b.txt", b"two", "text/plain")),
            ],
            data={"conversation_id": "conv_u1"},
        )
        assert resp.status_code == 400
        assert "Too many files" in resp.text

    @staticmethod
    def _asgi_post(
        app: Any,  # noqa: ANN401 (the ASGI application)
        chunks: list[bytes],
        headers: list[tuple[bytes, bytes]],
        *,
        authorization: bytes | None = b"Bearer u1",
        gap_seconds: float = 0.0,
        then_silence: bool = False,
    ) -> dict[str, Any]:
        """Drive the app at the ASGI level, where a body's headers can lie or be absent.

        ``gap_seconds`` waits before each chunk (a trickle); ``then_silence`` never sends
        the rest of the body once the chunks run out (a client that just stops).
        """
        sent: list[dict[str, Any]] = []
        pending = list(chunks)

        async def receive() -> dict[str, Any]:
            if gap_seconds:
                await asyncio.sleep(gap_seconds)
            if pending:
                chunk = pending.pop(0)
                more = bool(pending) or then_silence
                return {"type": "http.request", "body": chunk, "more_body": more}
            if then_silence:
                await asyncio.sleep(3600)
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/personas/astrid/uploads",
            "raw_path": b"/v1/personas/astrid/uploads",
            "query_string": b"",
            "root_path": "",
            "headers": [
                *([(b"authorization", authorization)] if authorization is not None else []),
                *headers,
            ],
            "client": ("127.0.0.1", 1234),
            "server": ("test", 80),
        }
        asyncio.run(app(scope, receive, send))
        start = next(m for m in sent if m["type"] == "http.response.start")
        return {"status": start["status"], "consumed": len(chunks) - len(pending)}

    def _oversize_chunks(self) -> list[bytes]:
        from persona_api.routes import uploads  # noqa: PLC0415

        head = (
            b'--b\r\nContent-Disposition: form-data; name="file"; filename="x.txt"\r\n'
            b"Content-Type: text/plain\r\n\r\n"
        )
        chunk = 1024 * 1024
        count = uploads.MAX_UPLOAD_BODY_BYTES // chunk + 4
        return [head] + [b"a" * chunk for _ in range(count)] + [b"\r\n--b--\r\n"]

    def test_a_chunked_body_over_the_limit_is_refused_while_streaming(
        self, client: TestClient
    ) -> None:
        chunks = self._oversize_chunks()
        result = self._asgi_post(
            client.app,
            chunks,
            [
                (b"content-type", b"multipart/form-data; boundary=b"),
                (b"transfer-encoding", b"chunked"),
            ],
        )
        assert result["status"] == 413
        assert result["consumed"] < len(chunks), "the whole body was read before refusing"

    def test_a_lying_content_length_is_refused_by_the_byte_count(self, client: TestClient) -> None:
        chunks = self._oversize_chunks()
        result = self._asgi_post(
            client.app,
            chunks,
            [(b"content-type", b"multipart/form-data; boundary=b"), (b"content-length", b"100")],
        )
        assert result["status"] == 413
        assert result["consumed"] < len(chunks), "the whole body was read before refusing"


class TestUploadBodyIsBoundedInTime:
    """F7 (second review): the caller is known before the body, and the body has a deadline."""

    _post = staticmethod(TestUploadRequestBodyLimit._asgi_post)

    @staticmethod
    def _body(size: int = 1024) -> list[bytes]:
        head = (
            b'--b\r\nContent-Disposition: form-data; name="file"; filename="memo.txt"\r\n'
            b"Content-Type: text/plain\r\n\r\n"
        )
        tail = (
            b'\r\n--b\r\nContent-Disposition: form-data; name="conversation_id"\r\n\r\n'
            b"conv_u1\r\n--b--\r\n"
        )
        return [head, b"a" * size, tail]

    _HEADERS = [(b"content-type", b"multipart/form-data; boundary=b")]

    def test_an_unauthenticated_upload_is_refused_before_any_body_is_read(
        self, client: TestClient
    ) -> None:
        chunks = self._body(1024 * 1024)
        result = self._post(client.app, chunks, self._HEADERS, authorization=None)
        assert result["status"] == 401
        assert result["consumed"] == 0

    def test_an_authenticated_upload_reads_its_body(self, client: TestClient) -> None:
        """Valid input passes: the same body with a token is read and stored."""
        chunks = self._body()
        result = self._post(client.app, chunks, self._HEADERS)
        assert result["status"] == 201
        assert result["consumed"] == len(chunks)

    def test_a_trickling_body_is_cut_off_at_the_deadline(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from persona_api.routes import uploads  # noqa: PLC0415

        monkeypatch.setattr(uploads, "MAX_UPLOAD_BODY_SECONDS", 0.5)
        chunks = [*self._body()[:1], *[b"a"] * 40, *self._body()[2:]]
        result = self._post(client.app, chunks, self._HEADERS, gap_seconds=0.05)
        assert result["status"] == 408
        assert result["consumed"] < len(chunks)

    def test_a_client_that_goes_silent_is_cut_off_at_the_deadline(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from persona_api.routes import uploads  # noqa: PLC0415

        monkeypatch.setattr(uploads, "MAX_UPLOAD_BODY_SECONDS", 0.5)
        result = self._post(client.app, self._body()[:1], self._HEADERS, then_silence=True)
        assert result["status"] == 408

    def test_a_slow_body_inside_the_deadline_is_accepted(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Valid input passes: slow is fine, as long as it arrives within the deadline."""
        from persona_api.routes import uploads  # noqa: PLC0415

        monkeypatch.setattr(uploads, "MAX_UPLOAD_BODY_SECONDS", 3.0)
        chunks = self._body()
        result = self._post(client.app, chunks, self._HEADERS, gap_seconds=0.2)
        assert result["status"] == 201

    def test_the_deadline_is_sixty_seconds(self) -> None:
        from persona_api.routes import uploads  # noqa: PLC0415

        assert uploads.MAX_UPLOAD_BODY_SECONDS == 60.0


class TestMissingParserLibrary:
    """When a parser library is absent the upload answers 503 ``missing_dependency``.

    This is what production answered for every pdf, docx and xlsx before the
    ``[documents]`` extra (research section 8.5). The body is FastAPI's
    ``{"detail": {...}}`` envelope, which the web's upload transport now unwraps.
    """

    @pytest.mark.parametrize(
        ("filename", "media_type", "library", "fmt"),
        [
            ("lease.pdf", "application/pdf", "pypdf", "pdf"),
            ("lease.docx", "application/octet-stream", "docx", "docx"),
            ("rent.xlsx", "application/octet-stream", "openpyxl", "xlsx"),
        ],
    )
    @pytest.mark.usefixtures("in_process_parser")
    def test_a_missing_parser_library_answers_503_and_stores_nothing(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        filename: str,
        media_type: str,
        library: str,
        fmt: str,
    ) -> None:
        """The library is hidden in this process, so the parse runs in this process."""
        import sys  # noqa: PLC0415

        if fmt == "pdf":
            body = (
                Path(__file__).resolve().parents[2]
                / "core"
                / "tests"
                / "fixtures"
                / "documents"
                / "sample-text.pdf"
            ).read_bytes()
        else:
            body = _office_bytes(fmt)
        root = _isolated_storage(client, tmp_path / "store")
        # ``import x`` raises ImportError when sys.modules maps x to None: exactly what a
        # production image without the extra does.
        monkeypatch.setitem(sys.modules, library, None)

        resp = client.post(
            "/v1/personas/astrid/uploads",
            headers=_auth(),
            files={"file": (filename, body, media_type)},
            data={"conversation_id": "conv_u1"},
        )

        assert resp.status_code == 503
        detail = resp.json()["detail"]
        assert detail["error"] == "missing_dependency"
        assert detail["context"]["format"] == fmt
        assert detail["context"]["install_hint"] == "pip install persona-core[documents]"
        assert _stored_files(root) == []

    @pytest.mark.parametrize("fmt", ["docx", "xlsx"])
    def test_with_the_library_present_the_same_upload_succeeds(
        self, client: TestClient, fmt: str
    ) -> None:
        """Precondition for the test above: the 503 is the library, not the file."""
        resp = client.post(
            "/v1/personas/astrid/uploads",
            headers=_auth(),
            files={"file": (f"doc.{fmt}", _office_bytes(fmt), "application/octet-stream")},
            data={"conversation_id": "conv_u1"},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["format"] == fmt


class TestHostileArchiveUpload:
    def test_a_lying_archive_is_refused_with_422_and_nothing_is_stored(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        import io  # noqa: PLC0415
        import struct  # noqa: PLC0415
        import zipfile  # noqa: PLC0415

        members: dict[str, bytes] = {}
        with zipfile.ZipFile(io.BytesIO(_office_bytes("docx"))) as archive:
            for info in archive.infolist():
                members[info.filename] = archive.read(info.filename)
        members["word/document.xml"] += b"<!--" + b"A" * (8 * 1024 * 1024) + b"-->"
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in members.items():
                archive.writestr(name, data)
        raw = bytearray(buffer.getvalue())
        # Declare the swollen part as 4 KiB in the central directory.
        end = raw.rfind(b"PK\x05\x06")
        (count,) = struct.unpack_from("<H", raw, end + 10)
        (offset,) = struct.unpack_from("<I", raw, end + 16)
        for _ in range(count):
            name_length, extra_length, comment_length = struct.unpack_from("<HHH", raw, offset + 28)
            if raw[offset + 46 : offset + 46 + name_length] == b"word/document.xml":
                struct.pack_into("<I", raw, offset + 24, 4096)
            offset += 46 + name_length + extra_length + comment_length

        root = _isolated_storage(client, tmp_path / "store")
        resp = client.post(
            "/v1/personas/astrid/uploads",
            headers=_auth(),
            files={"file": ("lease.docx", bytes(raw), "application/octet-stream")},
            data={"conversation_id": "conv_u1"},
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail["error"] == "corrupt_document"
        assert detail["context"]["reason"] == "size_mismatch"
        assert _stored_files(root) == []


class TestDocumentParseRunsOffTheEventLoop:
    """The parse is CPU-bound; on the loop it would stall the single api process."""

    def test_the_rls_owner_is_bound_inside_the_worker_thread(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """RLS fails closed, so a worker that lost the owner would read nothing.

        A custom executor via ``run_in_executor`` drops contextvars; this pins that the
        upload runs in another thread AND still sees the request's owner.
        """
        import threading  # noqa: PLC0415

        from persona_api.middleware.rls_context import current_user_id  # noqa: PLC0415
        from persona_api.services import document_service  # noqa: PLC0415

        seen: list[tuple[str | None, bool]] = []
        real_upload = document_service.upload

        def _recording_upload(**kwargs: Any) -> Any:  # noqa: ANN401
            seen.append(
                (current_user_id.get(), threading.current_thread() is threading.main_thread())
            )
            return real_upload(**kwargs)

        monkeypatch.setattr(document_service, "upload", _recording_upload)
        resp = client.post(
            "/v1/personas/astrid/uploads",
            headers=_auth(),
            files={"file": ("memo.txt", b"The lease runs for twelve months.", "text/plain")},
            data={"conversation_id": "conv_u1"},
        )
        assert resp.status_code == 201, resp.text
        assert len(seen) == 1, "precondition: the upload ran exactly once"
        owner, on_main_thread = seen[0]
        assert owner == "u1"
        assert not on_main_thread

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("in_process_parser")
    async def test_the_event_loop_keeps_running_while_a_parse_is_in_progress(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Proven by a release only the running loop can give, not by a clock.

        The stubbed parse blocks until ``release`` is set, and only a coroutine on the
        event loop sets it, after it sees the parse has started. If the parse ran on the
        loop, that coroutine could never run: the parse would give up after its timeout
        and the upload would fail.
        """
        import threading  # noqa: PLC0415

        import httpx  # noqa: PLC0415
        from persona_api.services import (
            document_parsing,  # noqa: PLC0415
        )

        started = threading.Event()
        release = threading.Event()
        real_parse = document_parsing.parse_untrusted_document

        def _slow_parse(path: Path, *, render_dir: Path | None, dpi: int | None) -> Any:  # noqa: ANN401
            started.set()
            if not release.wait(timeout=5):
                raise RuntimeError("the event loop never ran while the parse was in progress")
            return real_parse(path, render_dir=render_dir, dpi=dpi)

        monkeypatch.setattr(document_parsing, "parse_untrusted_document", _slow_parse)
        transport = httpx.ASGITransport(app=client.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            request = asyncio.create_task(
                http.post(
                    "/v1/personas/astrid/uploads",
                    headers=_auth(),
                    files={"file": ("memo.txt", b"The lease runs twelve months.", "text/plain")},
                    data={"conversation_id": "conv_u1"},
                )
            )
            loop_turns = 0
            while not started.is_set() and loop_turns < 500:
                await asyncio.sleep(0.01)
                loop_turns += 1
            assert started.is_set(), "precondition: the parse began"
            release.set()
            response = await request
        assert response.status_code == 201, response.text


class TestAbandonedUploadIsStoredAndAudited:
    """Review item 7: an upload whose client goes away is never stored without its audit."""

    @pytest.mark.asyncio
    async def test_cancelling_the_request_mid_store_still_records_the_audit(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import threading  # noqa: PLC0415

        import httpx  # noqa: PLC0415
        from persona_api.services import document_service  # noqa: PLC0415

        root = _isolated_storage(client, tmp_path / "store")
        storing = threading.Event()
        release = threading.Event()
        audited = threading.Event()
        audits: list[str] = []
        real_upload = document_service.upload

        def _slow_store(**kwargs: Any) -> Any:  # noqa: ANN401
            storing.set()
            release.wait(timeout=5)
            return real_upload(**kwargs)

        def _record(**kwargs: Any) -> None:  # noqa: ANN401
            audits.append(str(kwargs["action"]))
            audited.set()

        monkeypatch.setattr(document_service, "upload", _slow_store)
        monkeypatch.setattr(audit_service, "record", _record)

        transport = httpx.ASGITransport(app=client.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            request = asyncio.create_task(
                http.post(
                    "/v1/personas/astrid/uploads",
                    headers=_auth(),
                    files={"file": ("memo.txt", b"The lease runs twelve months.", "text/plain")},
                    data={"conversation_id": "conv_u1"},
                )
            )
            turns = 0
            while not storing.is_set() and turns < 500:
                await asyncio.sleep(0.01)
                turns += 1
            assert storing.is_set(), "precondition: the store step began"
            request.cancel()  # the client goes away while the store is running
            with pytest.raises(asyncio.CancelledError):
                await request
            release.set()
            assert await asyncio.to_thread(audited.wait, 5), "the audit row was never recorded"

        stored = [p.name for p in _stored_files(root)]
        assert any(name.endswith(".txt") for name in stored), stored
        assert audits == ["upload.create"]


class TestReparseGoesThroughTheParserPort:
    """Review item 8: an older whole-inject document is re-parsed through the same port."""

    def test_get_document_text_parses_through_the_port_without_rendering(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        """A document without an extracted-text sidecar (stored before D-FV0-31)."""
        from persona.documents.parsers import ParseResult  # noqa: PLC0415
        from persona_api.services import document_service  # noqa: PLC0415
        from persona_api.services.document_parsing import ParsedDocument  # noqa: PLC0415

        root = _isolated_storage(client, tmp_path / "store")
        resp = client.post(
            "/v1/personas/astrid/uploads",
            headers=_auth(),
            files={"file": ("memo.txt", b"The lease runs for twelve months.", "text/plain")},
            data={"conversation_id": "conv_u1"},
        )
        assert resp.status_code == 201, resp.text
        doc_ref = resp.json()["doc_ref"]
        sidecars = list(root.rglob(f"{doc_ref}.txt.text.json"))
        assert len(sidecars) == 1, "precondition: the upload stored its text"
        sidecars[0].unlink()  # now it is shaped like a document stored before the sidecar

        def _legacy_ref(root: Path, doc_ref: str) -> document_service.DocumentRef:
            (meta,) = root.rglob(f"{doc_ref}.txt.meta.json")
            ref = document_service.DocumentRef.model_validate_json(meta.read_text())
            return ref.model_copy(update={"text_stored": False})

        calls: list[tuple[str, Path | None, str, str]] = []

        class _RecordingParser:
            async def parse(
                self, path: Path, *, render_dir: Path | None, owner_id: str, purpose: str
            ) -> ParsedDocument:
                calls.append((path.suffix, render_dir, owner_id, purpose))
                from persona.documents.chunker import DocumentSection  # noqa: PLC0415

                return ParsedDocument(
                    parse_result=ParseResult(sections=(DocumentSection(text="from the port"),))
                )

        from persona_api.storage import LocalFileStorage  # noqa: PLC0415

        text = asyncio.run(
            document_service.get_document_text(
                file_storage=LocalFileStorage(root),
                owner_id="u1",
                persona_id="astrid",
                conversation_id="conv_u1",
                ref=_legacy_ref(root, doc_ref),
                parser=_RecordingParser(),
            )
        )
        assert text == "from the port"
        assert calls == [(".txt", None, "u1", "reparse")]

    def test_the_chat_route_hands_its_parser_to_the_document_context(self) -> None:
        """Structure: the one composition site passes the app's port, not a parser of its own."""
        import inspect  # noqa: PLC0415

        from persona_api.routes import conversations  # noqa: PLC0415

        source = inspect.getsource(conversations)
        assert "document_service.build_document_context(" in source
        call = source.split("document_service.build_document_context(", 1)[1].split(")", 1)[0]
        assert "parser=request.app.state.document_parser" in call


class TestUploadsThroughTheWorkerPool:
    """D-FV0-31 through the route: real workers, limits lowered so a test trips them fast."""

    @staticmethod
    def _use_pool(client: TestClient, **settings: Any) -> ProcessDocumentParser:  # noqa: ANN401
        pool = ProcessDocumentParser(
            workers=1,
            admission=client.app.state.document_admission,  # type: ignore[attr-defined]
            **settings,
        )
        client.app.state.document_parser = pool  # type: ignore[attr-defined]
        return pool

    @staticmethod
    def _bomb(tmp_path: Path) -> bytes:
        from _hostile_documents import flate_bomb_pdf  # noqa: PLC0415

        return flate_bomb_pdf(tmp_path / "bomb.pdf").read_bytes()

    def test_a_hostile_file_is_refused_cleanly_and_the_next_upload_works(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        pool = self._use_pool(client, deadline_seconds=2)
        root = _isolated_storage(client, tmp_path / "store")
        try:
            resp = client.post(
                "/v1/personas/astrid/uploads",
                headers=_auth(),
                files={"file": ("report.pdf", self._bomb(tmp_path), "application/pdf")},
                data={"conversation_id": "conv_u1"},
            )
            assert resp.status_code == 422, resp.text
            detail = resp.json()["detail"]
            assert detail["error"] == "corrupt_document"
            assert detail["context"] == {
                "format": "pdf",
                "reason": "parse_limit",
                "limit": "deadline",
            }
            assert _stored_files(root) == []

            # The next upload works (another user's); the hostile file's owner is cooling
            # down for a minute (F2) and is told when to come back.
            again = client.post(
                "/v1/personas/astrid/uploads",
                headers={"Authorization": "Bearer u2"},
                files={"file": ("memo.txt", b"The lease runs twelve months.", "text/plain")},
                data={"conversation_id": "conv_u1"},
            )
            assert again.status_code == 201, again.text
            cooling = client.post(
                "/v1/personas/astrid/uploads",
                headers=_auth(),
                files={"file": ("memo.txt", b"The lease runs twelve months.", "text/plain")},
                data={"conversation_id": "conv_u1"},
            )
            assert cooling.status_code == 429, cooling.text
            assert cooling.json()["detail"]["error"] == "parse_cooldown"
            assert 55 <= int(cooling.headers["Retry-After"]) <= 60
        finally:
            asyncio.run(pool.aclose())

    def test_the_reviewers_text_bomb_crosses_as_at_most_the_text_budget(
        self, client: TestClient, tmp_path: Path, document_store: DocumentStore
    ) -> None:
        """F1: 12 KB of PDF used to hand the api 60 million characters to ingest."""
        from _hostile_documents import text_bomb_pdf  # noqa: PLC0415
        from persona.documents.ingest import MAX_DOCUMENT_CHUNKS  # noqa: PLC0415
        from persona.documents.parsers import MAX_DOCUMENT_TEXT_CHARS  # noqa: PLC0415

        pool = self._use_pool(client)
        _isolated_storage(client, tmp_path / "store")
        bomb = text_bomb_pdf(tmp_path / "textbomb_60p_1MB.pdf").read_bytes()
        try:
            resp = client.post(
                "/v1/personas/astrid/uploads",
                headers=_auth(),
                files={"file": ("report.pdf", bomb, "application/pdf")},
                data={"conversation_id": "conv_u1"},
            )
        finally:
            asyncio.run(pool.aclose())
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["strategy"] == "retrieval"
        # Third review: the request embeds nothing. The document waits for its index.
        assert (body["index_state"], body["chunks_indexed"]) == ("pending", 0)
        assert document_store.get_all("conv_u1") == []
        from _document_upload import index_document  # noqa: PLC0415

        index_document(
            file_storage=client.app.state.file_storage,  # type: ignore[attr-defined]
            owner_id="u1",
            persona_id="astrid",
            conversation_id="conv_u1",
            doc_ref=body["doc_ref"],
            document_store=document_store,
        )
        chunks = document_store.get_all("conv_u1")
        assert 0 < len(chunks) <= MAX_DOCUMENT_CHUNKS
        assert sum(len(c.text) for c in chunks) < 2 * MAX_DOCUMENT_TEXT_CHARS

    def test_a_page_image_linked_to_a_secret_is_refused_and_nothing_is_stored(
        self, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """F5: the reviewer's ``symlink`` probe, through the real upload path."""
        from persona_api.document_worker import pool as pool_module  # noqa: PLC0415

        monkeypatch.setattr(pool_module, "WORKER_MODULE", "_fake_document_worker")
        monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parent))
        pool = self._use_pool(client)
        root = _isolated_storage(client, tmp_path / "store")
        try:
            resp = client.post(
                "/v1/personas/astrid/uploads",
                headers=_auth(),
                files={"file": ("scan.pdf", b"page:symlink", "application/pdf")},
                data={"conversation_id": "conv_u1"},
            )
        finally:
            asyncio.run(pool.aclose())
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["context"]["limit"] == "page_image"
        assert _stored_files(root) == []
        assert not client.app.state.document_admission.upload_in_flight("u1")  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_a_busy_pool_answers_503_with_retry_after_and_one_user_gets_429(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        import httpx  # noqa: PLC0415

        pool = self._use_pool(
            client, deadline_seconds=3, upload_wait_seconds=0.3, retry_after_seconds=9
        )
        # Any bearer token is its own user here (the fixture's verify_token).
        bomb = self._bomb(tmp_path)
        transport = httpx.ASGITransport(app=client.app)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:

                def post(user: str, name: str, body: bytes, kind: str) -> Any:  # noqa: ANN401
                    return http.post(
                        "/v1/personas/astrid/uploads",
                        headers={"Authorization": f"Bearer {user}"},
                        files={"file": (name, body, kind)},
                        data={"conversation_id": "conv_u1"},
                    )

                hostile = asyncio.create_task(post("u1", "report.pdf", bomb, "application/pdf"))
                await asyncio.sleep(0.5)
                same_user = await post("u1", "memo.txt", b"twelve months", "text/plain")
                other_user = await post("u2", "memo.txt", b"twelve months", "text/plain")
                assert (await hostile).status_code == 422
        finally:
            await pool.aclose()

        assert same_user.status_code == 429, same_user.text
        assert same_user.json()["detail"]["error"] == "upload_in_progress"
        assert other_user.status_code == 503, other_user.text
        assert other_user.json()["detail"]["error"] == "parser_busy"
        assert other_user.headers["Retry-After"] == "9"


class TestTheUploadSlotCoversStoreAndIngest:
    """F1/F2: an owner's upload slot is held until the store is done too.

    There is no ingest in the request any more (D-FV0-31, third review): the background
    index has its own per-owner quota, so the store charges no api time.
    """

    @pytest.mark.asyncio
    async def test_a_second_upload_during_the_first_ones_ingest_is_refused(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import threading  # noqa: PLC0415

        import httpx  # noqa: PLC0415
        from persona_api.services import document_service  # noqa: PLC0415

        storing = threading.Event()
        release = threading.Event()
        real_upload = document_service.upload

        def _slow_store(**kwargs: Any) -> Any:  # noqa: ANN401
            storing.set()
            release.wait(timeout=10)
            return real_upload(**kwargs)

        monkeypatch.setattr(document_service, "upload", _slow_store)
        admission = client.app.state.document_admission  # type: ignore[attr-defined]
        transport = httpx.ASGITransport(app=client.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:

            def post(name: str) -> Any:  # noqa: ANN401
                return http.post(
                    "/v1/personas/astrid/uploads",
                    headers=_auth(),
                    files={"file": (name, b"The lease runs twelve months.", "text/plain")},
                    data={"conversation_id": "conv_u1"},
                )

            first = asyncio.create_task(post("one.txt"))
            assert await asyncio.to_thread(storing.wait, 30), "precondition: the store began"
            second = await post("two.txt")
            assert second.status_code == 429, second.text
            assert second.json()["detail"]["error"] == "upload_in_progress"
            assert admission.upload_in_flight("u1")
            parse_seconds = admission.worker_seconds_used("u1")  # the parse is charged
            await asyncio.sleep(0.3)  # the store is held for at least this long
            release.set()
            assert (await first).status_code == 201
            assert not admission.upload_in_flight("u1")
            # Only the parse is charged: the store embeds nothing.
            assert admission.worker_seconds_used("u1") == pytest.approx(parse_seconds)
            third = await post("three.txt")
            assert third.status_code == 201, third.text


class _RecordingIndexScheduler:
    def __init__(self, *, full: bool = False) -> None:
        self.scheduled: list[dict[str, str]] = []
        self.picked: list[list[str]] = []
        self.admitted: list[str] = []
        self.full = full

    def admit(self, owner_id: str) -> None:
        from persona_api.services.document_index import (  # noqa: PLC0415
            DocumentIndexBacklogFullError,
        )

        self.admitted.append(owner_id)
        if self.full:
            raise DocumentIndexBacklogFullError(
                "full",
                context={"reason": "index_backlog_full", "retry_after_seconds": "60"},
            )

    async def schedule(self, **kwargs: str) -> None:
        self.scheduled.append(kwargs)

    async def pick_up(self, *, refs: Any, **_: str) -> None:  # noqa: ANN401
        self.picked.append([r.doc_ref for r in refs])


@pytest.mark.usefixtures("in_process_parser")
class TestTheUploadSchedulesTheIndex:
    """D-FV0-31: a retrieval upload returns at once, pending, with its index scheduled."""

    def test_a_retrieval_upload_is_pending_and_scheduled_and_embeds_nothing(
        self,
        client: TestClient,
        document_store: DocumentStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("PERSONA_DOC_INJECT_THRESHOLD", "100")
        scheduler = _RecordingIndexScheduler()
        client.app.state.document_index_scheduler = scheduler  # type: ignore[attr-defined]
        resp = client.post(
            "/v1/personas/astrid/uploads",
            headers=_auth(),
            files={"file": ("long.txt", ("Paragraph. " * 600).encode(), "text/plain")},
            data={"conversation_id": "conv_u1"},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert (body["strategy"], body["index_state"], body["chunks_indexed"]) == (
            "retrieval",
            "pending",
            0,
        )
        assert scheduler.scheduled == [
            {
                "owner_id": "u1",
                "persona_id": "astrid",
                "conversation_id": "conv_u1",
                "doc_ref": body["doc_ref"],
            }
        ]
        assert document_store.get_all("conv_u1") == []

    def test_a_whole_inject_upload_schedules_nothing(self, client: TestClient) -> None:
        scheduler = _RecordingIndexScheduler()
        client.app.state.document_index_scheduler = scheduler  # type: ignore[attr-defined]
        resp = client.post(
            "/v1/personas/astrid/uploads",
            headers=_auth(),
            files={"file": ("memo.txt", b"The lease runs twelve months.", "text/plain")},
            data={"conversation_id": "conv_u1"},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["index_state"] == "not_needed"
        assert scheduler.scheduled == []

    def test_a_scheduler_that_fails_does_not_fail_the_upload(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PERSONA_DOC_INJECT_THRESHOLD", "100")

        class _Broken(_RecordingIndexScheduler):
            async def schedule(self, **_: str) -> None:
                raise RuntimeError("the queue is down")

        client.app.state.document_index_scheduler = _Broken()  # type: ignore[attr-defined]
        resp = client.post(
            "/v1/personas/astrid/uploads",
            headers=_auth(),
            files={"file": ("long.txt", ("Paragraph. " * 600).encode(), "text/plain")},
            data={"conversation_id": "conv_u1"},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["index_state"] == "pending"  # a turn or a listing picks it up

    def test_an_upload_past_the_backlog_cap_is_refused_and_nothing_is_stored(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, workspace_root: Path
    ) -> None:
        """Fourth review finding 1: at most 20 documents wait for the index per owner."""
        monkeypatch.setenv("PERSONA_DOC_INJECT_THRESHOLD", "100")
        scheduler = _RecordingIndexScheduler(full=True)
        client.app.state.document_index_scheduler = scheduler  # type: ignore[attr-defined]
        storage = _isolated_storage(client, workspace_root / "isolated")
        resp = client.post(
            "/v1/personas/astrid/uploads",
            headers=_auth(),
            files={"file": ("long.txt", ("Paragraph. " * 600).encode(), "text/plain")},
            data={"conversation_id": "conv_u1"},
        )
        assert resp.status_code == 429, resp.text
        assert resp.json()["detail"]["error"] == "index_backlog_full"
        assert resp.headers["Retry-After"] == "60"
        assert scheduler.admitted == ["u1"]
        assert scheduler.scheduled == []
        assert _stored_files(storage) == []

    def test_a_short_document_is_not_held_by_a_full_backlog(self, client: TestClient) -> None:
        scheduler = _RecordingIndexScheduler(full=True)
        client.app.state.document_index_scheduler = scheduler  # type: ignore[attr-defined]
        resp = client.post(
            "/v1/personas/astrid/uploads",
            headers=_auth(),
            files={"file": ("memo.txt", b"The lease runs twelve months.", "text/plain")},
            data={"conversation_id": "conv_u1"},
        )
        assert resp.status_code == 201, resp.text
        assert scheduler.admitted == []  # only a document that will be indexed asks

    def test_listing_the_documents_is_rate_limited(self, client: TestClient) -> None:
        from persona_api.middleware.rate_limit import (  # noqa: PLC0415
            InMemoryRateLimitStore,
            RateLimiter,
        )

        client.app.state.rate_limiter = RateLimiter(  # type: ignore[attr-defined]
            InMemoryRateLimitStore(), default_limit=1, per_endpoint={}
        )
        assert client.get("/v1/conversations/conv_u1/documents", headers=_auth()).status_code == 200
        assert client.get("/v1/conversations/conv_u1/documents", headers=_auth()).status_code == 429

    def test_listing_the_documents_picks_up_those_waiting(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PERSONA_DOC_INJECT_THRESHOLD", "100")
        client.post(
            "/v1/personas/astrid/uploads",
            headers=_auth(),
            files={"file": ("long.txt", ("Paragraph. " * 600).encode(), "text/plain")},
            data={"conversation_id": "conv_u1"},
        )
        scheduler = _RecordingIndexScheduler()
        client.app.state.document_index_scheduler = scheduler  # type: ignore[attr-defined]
        listed = client.get("/v1/conversations/conv_u1/documents", headers=_auth())
        assert listed.status_code == 200
        assert scheduler.picked == [[d["doc_ref"] for d in listed.json()]]


async def _asgi_upload(
    app: Any,  # noqa: ANN401 (the ASGI application)
    chunks: list[bytes],
    *,
    user: str = "u1",
    hold: asyncio.Event | None = None,
    holding: asyncio.Event | None = None,
) -> dict[str, Any]:
    """One upload at the ASGI level, several at once on one loop.

    With ``hold``, the body stops after its first chunk (``holding`` is set then) until
    ``hold`` is set: a body still arriving.
    """
    sent: list[dict[str, Any]] = []
    pending = list(chunks)

    async def receive() -> dict[str, Any]:
        if hold is not None and len(pending) == len(chunks) - 1:
            if holding is not None:
                holding.set()
            await hold.wait()
        if pending:
            chunk = pending.pop(0)
            return {"type": "http.request", "body": chunk, "more_body": bool(pending)}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/personas/astrid/uploads",
        "raw_path": b"/v1/personas/astrid/uploads",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"authorization", f"Bearer {user}".encode()),
            (b"content-type", b"multipart/form-data; boundary=b"),
        ],
        "client": ("127.0.0.1", 1234),
        "server": ("test", 80),
    }
    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return {"status": start["status"], "consumed": len(chunks) - len(pending), "body": body}


def _memo_body(text: bytes = b"The lease runs twelve months.") -> list[bytes]:
    head = (
        b'--b\r\nContent-Disposition: form-data; name="file"; filename="memo.txt"\r\n'
        b"Content-Type: text/plain\r\n\r\n"
    )
    tail = (
        b'\r\n--b\r\nContent-Disposition: form-data; name="conversation_id"\r\n\r\n'
        b"conv_u1\r\n--b--\r\n"
    )
    return [head, text, tail]


class TestUploadsAreBoundedBeforeTheBody:
    """Third review finding 5: the rate limit and a per-owner body cap, before the body."""

    @pytest.mark.asyncio
    async def test_a_third_body_from_one_owner_is_refused_before_any_of_it_is_read(
        self, client: TestClient
    ) -> None:
        import json  # noqa: PLC0415

        from persona_api.document_worker.admission import MAX_BODIES_PER_OWNER  # noqa: PLC0415

        assert MAX_BODIES_PER_OWNER == 2
        app = client.app
        admission = app.state.document_admission  # type: ignore[attr-defined]
        hold = asyncio.Event()
        holding = [asyncio.Event(), asyncio.Event()]
        try:
            arriving = [
                asyncio.create_task(_asgi_upload(app, _memo_body(), hold=hold, holding=flag))
                for flag in holding
            ]
            for flag in holding:
                await asyncio.wait_for(flag.wait(), 30)
            assert admission.bodies_arriving("u1") == 2
            third = await _asgi_upload(app, _memo_body())
            other_owner = await _asgi_upload(app, _memo_body(), user="u2")
        finally:
            hold.set()
        statuses = sorted(r["status"] for r in await asyncio.gather(*arriving))
        assert third["status"] == 429
        assert third["consumed"] == 0
        assert json.loads(third["body"])["detail"]["error"] == "uploads_arriving"
        assert other_owner["status"] == 201, other_owner["body"]
        assert statuses[0] == 201  # the two held bodies were read and handled
        assert statuses[1] in (201, 429)  # 429 only as the one-upload-in-flight rule
        assert admission.bodies_arriving("u1") == 0
        assert (await _asgi_upload(app, _memo_body()))["status"] == 201

    @pytest.mark.asyncio
    async def test_an_upload_still_being_stored_holds_no_body_place(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The cap is on bodies arriving, not on uploads in flight."""
        import threading  # noqa: PLC0415

        from persona_api.services import document_service  # noqa: PLC0415

        storing = threading.Event()
        release = threading.Event()
        real_upload = document_service.upload

        def _slow_store(**kwargs: Any) -> Any:  # noqa: ANN401
            storing.set()
            release.wait(timeout=30)
            return real_upload(**kwargs)

        monkeypatch.setattr(document_service, "upload", _slow_store)
        admission = client.app.state.document_admission  # type: ignore[attr-defined]
        upload = asyncio.create_task(_asgi_upload(client.app, _memo_body()))
        try:
            assert await asyncio.to_thread(storing.wait, 30), "precondition: the store began"
            assert admission.upload_in_flight("u1")
            assert admission.bodies_arriving("u1") == 0
        finally:
            release.set()
        assert (await upload)["status"] == 201

    @pytest.mark.asyncio
    async def test_a_body_frees_its_place_once_it_has_arrived(self) -> None:
        from persona_api.routes.uploads import _counting_receive  # noqa: PLC0415

        messages = [
            {"type": "http.request", "body": b"a", "more_body": True},
            {"type": "http.request", "body": b"b", "more_body": False},
            {"type": "http.disconnect"},
        ]
        done: list[int] = []

        async def receive() -> dict[str, Any]:
            return messages.pop(0)

        counted = _counting_receive(receive, lambda: done.append(len(messages)))
        await counted()
        assert done == []  # still arriving
        await counted()
        assert done == [1]  # the last chunk is in
        await counted()
        assert done == [1]  # once

    def test_the_rate_limit_refuses_before_any_of_the_body_is_read(
        self, client: TestClient
    ) -> None:
        from persona_api.middleware.rate_limit import (  # noqa: PLC0415
            InMemoryRateLimitStore,
            RateLimiter,
        )

        client.app.state.rate_limiter = RateLimiter(  # type: ignore[attr-defined]
            InMemoryRateLimitStore(), default_limit=1, per_endpoint={}
        )
        first = asyncio.run(_asgi_upload(client.app, _memo_body()))
        # One upload is counted once: the route's dependency reuses the early decision.
        assert first["status"] == 201, first["body"]
        refused = asyncio.run(_asgi_upload(client.app, _memo_body()))
        assert refused["status"] == 429
        assert refused["consumed"] == 0
        other_owner = asyncio.run(_asgi_upload(client.app, _memo_body(), user="u2"))
        assert other_owner["status"] == 201


class TestACancelledUploadIsNotTheOwnersFault:
    """Third review finding 7: a disconnect during render costs the owner nothing."""

    @pytest.mark.asyncio
    async def test_the_scratch_directory_outlives_the_request_until_the_worker_is_done(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import shutil  # noqa: PLC0415
        import threading  # noqa: PLC0415

        import httpx  # noqa: PLC0415
        from persona_api.document_worker import pool as pool_module  # noqa: PLC0415
        from persona_api.routes import uploads  # noqa: PLC0415

        monkeypatch.setattr(pool_module, "WORKER_MODULE", "_fake_document_worker")
        monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parent))
        admission = client.app.state.document_admission  # type: ignore[attr-defined]
        pool = ProcessDocumentParser(workers=1, admission=admission, spare_worker=False)
        client.app.state.document_parser = pool  # type: ignore[attr-defined]
        made: list[str] = []
        removed = threading.Event()
        real_mkdtemp = uploads.tempfile.mkdtemp
        real_rmtree = shutil.rmtree

        def _mkdtemp(**kwargs: Any) -> str:  # noqa: ANN401
            path = real_mkdtemp(**kwargs)
            if kwargs.get("prefix") == "persona-upload-":  # not a worker's own directory
                made.append(path)
            return path

        def _rmtree(path: str, **kwargs: Any) -> None:  # noqa: ANN401
            real_rmtree(path, **kwargs)
            if path in made:
                removed.set()

        monkeypatch.setattr(uploads.tempfile, "mkdtemp", _mkdtemp)
        monkeypatch.setattr(uploads.shutil, "rmtree", _rmtree)
        transport = httpx.ASGITransport(app=client.app)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
                upload = asyncio.create_task(
                    http.post(
                        "/v1/personas/astrid/uploads",
                        headers=_auth(),
                        files={"file": ("scan.txt", b"render_after:1.0", "text/plain")},
                        data={"conversation_id": "conv_u1"},
                    )
                )
                await asyncio.sleep(0.5)
                upload.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await upload
                (scratch,) = made
                assert Path(scratch).is_dir()  # the worker is still rendering into it
                assert await asyncio.to_thread(removed.wait, 10), "never removed"
                assert not Path(scratch).exists()
                assert not admission.upload_in_flight("u1")
                assert not admission.is_suspect("u1")
                admission.check("u1")  # no cooldown
                again = await http.post(
                    "/v1/personas/astrid/uploads",
                    headers=_auth(),
                    files={"file": ("memo.txt", b"echo", "text/plain")},
                    data={"conversation_id": "conv_u1"},
                )
                assert again.status_code == 201, again.text
        finally:
            await pool.aclose()
