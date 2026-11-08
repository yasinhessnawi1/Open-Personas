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
from typing import TYPE_CHECKING, Any

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
        self.delete_batches: list[int] = []
        self.refuse_deletes = False
        self.delete_error: Exception | None = None
        self.list_error: Exception | None = None

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

    def delete_objects(self, *, Bucket: str, Delete: dict[str, Any]) -> dict[str, object]:  # noqa: N803
        objects = Delete["Objects"]
        self.delete_batches.append(len(objects))
        if self.delete_error is not None:
            raise self.delete_error
        if self.refuse_deletes:
            return {"Errors": [{"Key": o["Key"], "Code": "AccessDenied"} for o in objects]}
        for obj in objects:
            self.store.pop((Bucket, obj["Key"]), None)
        return {}

    def get_paginator(self, _name: str) -> _FakePaginator:
        return _FakePaginator(self)


class _FakePaginator:
    def __init__(self, client: _FakeS3Client) -> None:
        self._client = client

    def paginate(self, *, Bucket: str, Prefix: str) -> object:  # noqa: N803
        # Several pages, smaller than a delete batch, as list_objects_v2 really answers: a caller
        # that read only the first page would leave objects behind.
        if self._client.list_error is not None:
            raise self._client.list_error
        contents = [
            {"Key": key, "Size": len(body)}
            for (bkt, key), body in self._client.store.items()
            if bkt == Bucket and key.startswith(Prefix)
        ]
        for start in range(0, len(contents), 400):
            yield {"Contents": contents[start : start + 400]}
        if not contents:
            yield {}


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


# --------------------------------------------------------------------------- #
# delete_prefix: everything a persona or an account stored (R9-284)            #
# --------------------------------------------------------------------------- #
_REFUSED_PREFIXES = [
    "",
    "/",
    ".",
    "..",
    "u1/..",
    "a/../b",
    "../u2",
    "/u1",
    "u1",
    "u1/",
    "u1//",
    "u1/p1//",
    "u1\\p1",
]


def _seed_two_tenants(storage: FileStorage) -> None:
    storage.put("u1/p1/uploads/a.png", b"aaa")
    storage.put("u1/p1/uploads/a.png.f5.json", b"{}")
    storage.put("u1/p1/conversations/c1/documents/d.md", b"d")
    storage.put("u1/p10/uploads/sibling.png", b"s")  # starts with the same characters
    storage.put("u2/p9/uploads/c.png", b"c")


def test_local_delete_prefix_removes_the_folder_and_says_how_many_files_went(
    tmp_path: Path,
) -> None:
    storage = LocalFileStorage(tmp_path)
    _seed_two_tenants(storage)

    assert storage.delete_prefix("u1/p1/") == 3

    assert not (tmp_path / "u1" / "p1").exists()
    assert (tmp_path / "u1" / "p10" / "uploads" / "sibling.png").exists()
    assert (tmp_path / "u2" / "p9" / "uploads" / "c.png").exists()


def test_local_delete_prefix_is_idempotent(tmp_path: Path) -> None:
    storage = LocalFileStorage(tmp_path)
    _seed_two_tenants(storage)
    storage.delete_prefix("u1/p1")

    assert storage.delete_prefix("u1/p1") == 0
    assert storage.delete_prefix("never/written") == 0


@pytest.mark.parametrize("prefix", _REFUSED_PREFIXES)
def test_local_delete_prefix_refuses_anything_but_a_plain_folder(
    tmp_path: Path, prefix: str
) -> None:
    storage = LocalFileStorage(tmp_path)
    _seed_two_tenants(storage)
    with pytest.raises(SandboxViolationError):
        storage.delete_prefix(prefix)
    assert (tmp_path / "u1" / "p1" / "uploads" / "a.png").exists()
    assert (tmp_path / "u2" / "p9" / "uploads" / "c.png").exists()


def test_local_delete_prefix_refuses_a_dotted_path_for_what_it_is(tmp_path: Path) -> None:
    """``u1/..`` resolves to the root: the plain-path check has to be what stops it."""
    storage = LocalFileStorage(tmp_path)
    _seed_two_tenants(storage)
    with pytest.raises(SandboxViolationError) as caught:
        storage.delete_prefix("u1/..")
    assert caught.value.context["reason"] == "not_a_plain_path"


def test_local_delete_prefix_refuses_a_folder_that_resolves_somewhere_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What POSIX resolution hands back for a link, simulated so every host runs this.

    On Windows the resolver refuses a link itself, so without this the check below it would
    only ever be exercised on Linux.
    """
    storage = LocalFileStorage(tmp_path)
    _seed_two_tenants(storage)
    target = tmp_path.resolve() / "u2" / "p9"
    monkeypatch.setattr(storage, "_resolve", lambda _key: target)

    with pytest.raises(SandboxViolationError) as caught:
        storage.delete_prefix("u1/alias")

    assert caught.value.context["reason"] == "link"
    assert (tmp_path / "u2" / "p9" / "uploads" / "c.png").exists()


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except OSError:
        pytest.skip("this host cannot create symbolic links")


def test_local_delete_prefix_removes_a_link_inside_without_following_it(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    storage = LocalFileStorage(root)
    _seed_two_tenants(storage)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_bytes(b"keep")
    _symlink_or_skip(root / "u1" / "p1" / "planted", outside)

    storage.delete_prefix("u1/p1")

    assert not (root / "u1" / "p1").exists()
    assert (outside / "keep.txt").exists()


def test_local_delete_prefix_refuses_a_prefix_that_is_itself_a_link(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    storage = LocalFileStorage(root)
    _seed_two_tenants(storage)
    _symlink_or_skip(root / "u1" / "alias", root / "u2" / "p9")

    with pytest.raises(SandboxViolationError):
        storage.delete_prefix("u1/alias")

    assert (root / "u2" / "p9" / "uploads" / "c.png").exists()


_REFUSED_OWNERS = ["", ".", "..", "u1/", "u1/p1", "../u2", "/u1", "u1\\p1"]


def test_an_empty_persona_cannot_widen_a_prefix_to_the_whole_account(tmp_path: Path) -> None:
    """The review's HIGH: ``f"{owner}/{persona}/"`` with no persona is the owner's folder."""
    storage = LocalFileStorage(tmp_path)
    _seed_two_tenants(storage)
    for prefix in ("u1", "u1/", "u1//"):
        with pytest.raises(SandboxViolationError):
            storage.delete_prefix(prefix)
    assert (tmp_path / "u1" / "p1" / "uploads" / "a.png").exists()
    assert (tmp_path / "u1" / "p10" / "uploads" / "sibling.png").exists()


def test_local_account_door_removes_exactly_that_owner(tmp_path: Path) -> None:
    storage = LocalFileStorage(tmp_path)
    _seed_two_tenants(storage)

    assert storage.delete_account_folder("u1") == 4

    assert not (tmp_path / "u1").exists()
    assert (tmp_path / "u2" / "p9" / "uploads" / "c.png").exists()
    assert storage.delete_account_folder("u1") == 0


@pytest.mark.parametrize("owner", _REFUSED_OWNERS)
def test_local_account_door_takes_one_owner_name_and_nothing_else(
    tmp_path: Path, owner: str
) -> None:
    storage = LocalFileStorage(tmp_path)
    _seed_two_tenants(storage)
    with pytest.raises(SandboxViolationError):
        storage.delete_account_folder(owner)
    assert (tmp_path / "u1" / "p1" / "uploads" / "a.png").exists()
    assert (tmp_path / "u2" / "p9" / "uploads" / "c.png").exists()


@pytest.mark.usefixtures("_fake_botocore")
def test_s3_account_door_removes_exactly_that_owner() -> None:
    storage, client = _s3_with_fake_client()
    _seed_two_tenants(storage)
    client.store[("bkt", "u10/p1/uploads/similar.png")] = b"x"

    assert storage.delete_account_folder("u1") == 4

    assert {key for _, key in client.store} == {
        "u2/p9/uploads/c.png",
        "u10/p1/uploads/similar.png",
    }


@pytest.mark.usefixtures("_fake_botocore")
@pytest.mark.parametrize("owner", _REFUSED_OWNERS)
def test_s3_account_door_takes_one_owner_name_and_nothing_else(owner: str) -> None:
    storage, client = _s3_with_fake_client()
    _seed_two_tenants(storage)
    before = dict(client.store)
    with pytest.raises(SandboxViolationError):
        storage.delete_account_folder(owner)
    assert client.store == before


@pytest.mark.usefixtures("_fake_botocore")
def test_s3_delete_prefix_removes_only_that_folder_and_says_how_many() -> None:
    storage, client = _s3_with_fake_client()
    _seed_two_tenants(storage)

    assert storage.delete_prefix("u1/p1/") == 3

    keys = {key for _, key in client.store}
    assert keys == {"u1/p10/uploads/sibling.png", "u2/p9/uploads/c.png"}
    assert storage.delete_prefix("u1/p1") == 0


@pytest.mark.usefixtures("_fake_botocore")
def test_s3_delete_prefix_sends_batches_the_api_accepts() -> None:
    storage, client = _s3_with_fake_client()
    for i in range(2500):
        client.store[("bkt", f"u1/p1/uploads/{i}.png")] = b"x"

    assert storage.delete_prefix("u1/p1") == 2500

    assert client.delete_batches == [1000, 1000, 500]
    assert not client.store


@pytest.mark.usefixtures("_fake_botocore")
def test_s3_delete_prefix_says_so_when_the_store_refused() -> None:
    storage, client = _s3_with_fake_client()
    _seed_two_tenants(storage)
    client.refuse_deletes = True

    with pytest.raises(OSError, match="3"):
        storage.delete_prefix("u1/p1")


@pytest.mark.usefixtures("_fake_botocore")
@pytest.mark.parametrize("failing", ["delete_error", "list_error"])
def test_s3_delete_prefix_reports_a_store_failure_as_oserror_without_its_message(
    failing: str,
) -> None:
    """The documented contract, so a caller handles one error type on both backends."""
    storage, client = _s3_with_fake_client()
    _seed_two_tenants(storage)
    setattr(client, failing, _FakeClientError("u1/p1/uploads/a-private-file-name.png"))

    with pytest.raises(OSError, match="object store") as caught:
        storage.delete_prefix("u1/p1")

    assert "private-file-name" not in str(caught.value)
    assert caught.value.__suppress_context__ is True, "a traceback would print the store's message"


@pytest.mark.usefixtures("_fake_botocore")
@pytest.mark.parametrize("prefix", _REFUSED_PREFIXES)
def test_s3_delete_prefix_refuses_anything_but_a_plain_folder(prefix: str) -> None:
    """An empty prefix on S3 would be the whole bucket."""
    storage, client = _s3_with_fake_client()
    _seed_two_tenants(storage)
    before = dict(client.store)
    with pytest.raises(SandboxViolationError):
        storage.delete_prefix(prefix)
    assert client.store == before


def _import_available(name: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(name) is not None
