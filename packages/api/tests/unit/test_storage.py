"""R5-D-4: the FileStorage seam — local byte-identical, S3 shape, env-gated.

Local is the load-bearing default: same path, same bytes, same traversal
rejection as the pre-R5 inline writes. The S3 backend's provider round-trip is a
named R4 operator leg (real Tigris) — here we prove the SHAPE (put/get/exists/
delete/open_stream + sidecar-as-sibling-key) against a fake client, and that the
optional boto3 dep is genuinely optional (a helpful error, not an ImportError
crash, when s3 is selected without it).
"""

from __future__ import annotations

import sys
import types
from typing import TYPE_CHECKING

import pytest
from persona.errors import SandboxViolationError
from persona_api.config import APIConfig
from persona_api.storage import (
    FileStorage,
    LocalFileStorage,
    S3FileStorage,
    build_file_storage,
)

if TYPE_CHECKING:
    from pathlib import Path

_KEY = "u1/p1/uploads/abc123.png"
_SIDECAR = _KEY + ".f5.json"


# --------------------------------------------------------------------------- #
# LocalFileStorage — the byte-identical default                               #
# --------------------------------------------------------------------------- #
def test_local_put_is_byte_identical_at_the_expected_path(tmp_path: Path) -> None:
    storage = LocalFileStorage(tmp_path)
    ref = storage.put(_KEY, b"the-bytes", content_type="image/png")
    assert ref == _KEY
    # Same on-disk layout the inline O_NOFOLLOW write produced: root/{key}.
    on_disk = tmp_path / "u1" / "p1" / "uploads" / "abc123.png"
    assert on_disk.read_bytes() == b"the-bytes"
    assert storage.get(_KEY) == b"the-bytes"
    assert b"".join(storage.open_stream(_KEY)) == b"the-bytes"
    assert storage.exists(_KEY)
    assert not storage.exists("u1/p1/uploads/missing.png")


def test_local_sidecar_is_a_sibling_key(tmp_path: Path) -> None:
    storage = LocalFileStorage(tmp_path)
    storage.put(_KEY, b"img")
    storage.put(_SIDECAR, b'{"source":"generated"}')
    # The sidecar lands next to the bytes — the artifact walk finds both.
    assert (tmp_path / "u1" / "p1" / "uploads" / "abc123.png.f5.json").exists()
    assert storage.get(_SIDECAR) == b'{"source":"generated"}'


def test_local_delete_reports_prior_existence(tmp_path: Path) -> None:
    storage = LocalFileStorage(tmp_path)
    storage.put(_KEY, b"x")
    assert storage.delete(_KEY) is True
    assert storage.delete(_KEY) is False


def test_local_rejects_path_traversal(tmp_path: Path) -> None:
    storage = LocalFileStorage(tmp_path)
    with pytest.raises(SandboxViolationError):
        storage.put("../escape.png", b"nope")


def test_local_satisfies_the_protocol(tmp_path: Path) -> None:
    assert isinstance(LocalFileStorage(tmp_path), FileStorage)


# --------------------------------------------------------------------------- #
# build_file_storage — env-gated selection + graceful fallback                #
# --------------------------------------------------------------------------- #
def test_factory_defaults_to_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PERSONA_API_STORAGE_BACKEND", raising=False)
    assert isinstance(build_file_storage(APIConfig(), tmp_path), LocalFileStorage)


def test_factory_s3_without_bucket_falls_back_to_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PERSONA_API_STORAGE_BACKEND", "s3")
    monkeypatch.delenv("PERSONA_API_STORAGE_S3_BUCKET", raising=False)
    # No bucket → the misconfigured S3 backend must not brick boot; fall back.
    assert isinstance(build_file_storage(APIConfig(), tmp_path), LocalFileStorage)


def test_factory_s3_without_boto3_raises_a_helpful_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if "boto3" in sys.modules or _import_available("boto3"):
        pytest.skip("boto3 is installed; the guarded-import branch is unobservable")
    monkeypatch.setenv("PERSONA_API_STORAGE_BACKEND", "s3")
    monkeypatch.setenv("PERSONA_API_STORAGE_S3_BUCKET", "my-bucket")
    with pytest.raises(RuntimeError, match="boto3"):
        build_file_storage(APIConfig(), tmp_path)


# --------------------------------------------------------------------------- #
# S3FileStorage — the SHAPE, against a fake client (real Tigris = operator leg) #
# --------------------------------------------------------------------------- #
class _FakeBody:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data

    def iter_chunks(self, chunk_size: int = 1024) -> object:
        for i in range(0, len(self._data), chunk_size):
            yield self._data[i : i + chunk_size]


class _FakeClientError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _FakeS3Client:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], bytes] = {}

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, ContentType: str) -> None:  # noqa: N803 — boto3 kwarg names
        _ = ContentType
        self.store[(Bucket, Key)] = Body

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, object]:  # noqa: N803
        return {"Body": _FakeBody(self.store[(Bucket, Key)])}

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, object]:  # noqa: N803
        if (Bucket, Key) not in self.store:
            raise _FakeClientError("404")
        return {}

    def delete_object(self, *, Bucket: str, Key: str) -> None:  # noqa: N803
        self.store.pop((Bucket, Key), None)

    def get_paginator(self, _name: str) -> _FakePaginator:
        return _FakePaginator(self)


class _FakePaginator:
    def __init__(self, client: _FakeS3Client) -> None:
        self._client = client

    def paginate(self, *, Bucket: str, Prefix: str) -> object:  # noqa: N803
        # One page is enough for the shape test; mirror list_objects_v2's contract.
        contents = [
            {"Key": key, "Size": len(body)}
            for (bkt, key), body in self._client.store.items()
            if bkt == Bucket and key.startswith(Prefix)
        ]
        yield {"Contents": contents}


@pytest.fixture
def _fake_botocore(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject a minimal ``botocore.exceptions`` so ``exists`` can catch ClientError
    without the real (absent) dependency."""
    mod = types.ModuleType("botocore.exceptions")
    mod.ClientError = _FakeClientError  # type: ignore[attr-defined]
    pkg = types.ModuleType("botocore")
    monkeypatch.setitem(sys.modules, "botocore", pkg)
    monkeypatch.setitem(sys.modules, "botocore.exceptions", mod)


def _s3_with_fake_client() -> tuple[S3FileStorage, _FakeS3Client]:
    storage = S3FileStorage.__new__(S3FileStorage)  # bypass the boto3 import in __init__
    client = _FakeS3Client()
    storage._bucket = "bkt"  # noqa: SLF001 — white-box injection for the shape test
    storage._client = client  # noqa: SLF001
    return storage, client


@pytest.mark.usefixtures("_fake_botocore")
def test_s3_shape_put_get_exists_delete_stream_and_sidecar_sibling() -> None:
    storage, client = _s3_with_fake_client()

    assert storage.put(_KEY, b"img-bytes", content_type="image/png") == _KEY
    # Sidecar is an ordinary sibling OBJECT key — the S3 artifact walk finds it.
    storage.put(_SIDECAR, b'{"source":"generated"}')
    assert ("bkt", _KEY) in client.store
    assert ("bkt", _SIDECAR) in client.store

    assert storage.get(_KEY) == b"img-bytes"
    assert b"".join(storage.open_stream(_KEY)) == b"img-bytes"
    assert storage.exists(_KEY) is True
    assert storage.exists("u1/p1/uploads/missing.png") is False
    assert storage.delete(_KEY) is True
    assert storage.delete(_KEY) is False


# --------------------------------------------------------------------------- #
# list(prefix) — the S3-correct listing (Local walk / S3 list_objects_v2)      #
# --------------------------------------------------------------------------- #
def test_local_list_is_prefix_scoped_and_includes_sidecars(tmp_path: Path) -> None:
    storage = LocalFileStorage(tmp_path)
    storage.put("u1/p1/uploads/a.png", b"aaa")
    storage.put("u1/p1/uploads/a.png.f5.json", b"{}")  # sidecar — included
    storage.put("u1/p1/out/b.txt", b"bb")
    storage.put("u2/p9/uploads/c.png", b"c")  # other persona — excluded
    objs = {o.key: o.size for o in storage.list("u1/p1")}
    assert objs == {
        "u1/p1/uploads/a.png": 3,
        "u1/p1/uploads/a.png.f5.json": 2,
        "u1/p1/out/b.txt": 2,
    }


def test_local_list_missing_prefix_is_empty(tmp_path: Path) -> None:
    assert list(LocalFileStorage(tmp_path).list("nope/nothing")) == []


@pytest.mark.usefixtures("_fake_botocore")
def test_s3_list_uses_the_paginator_and_returns_objects() -> None:
    storage, _client = _s3_with_fake_client()
    storage.put("u1/p1/uploads/a.png", b"aaa")
    storage.put("u1/p1/uploads/a.png.f5.json", b"{}")
    storage.put("u2/p9/uploads/c.png", b"c")  # different prefix — excluded
    objs = {o.key: o.size for o in storage.list("u1/p1")}
    assert objs == {"u1/p1/uploads/a.png": 3, "u1/p1/uploads/a.png.f5.json": 2}


def _import_available(name: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(name) is not None
