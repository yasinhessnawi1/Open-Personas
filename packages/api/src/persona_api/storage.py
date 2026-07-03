"""External file-storage seam — local default, S3-generic pluggable (Spec R5, R5-D-4).

Today every produced artifact / upload / image lands on the ONE Fly volume at
``/var/lib/persona`` via ``resolve_sandbox_path`` + an ``O_NOFOLLOW`` write. Fly
volumes are one-Machine / host-pinned / region-pinned and DO NOT replicate, so
the moment the API runs on >1 instance an artifact written on machine A is simply
absent on machine B — the storage layer physically cannot serve multi-instance.

This module is the seam that lifts that. ``FileStorage`` is a small Protocol over
a content store keyed by a **logical key** (``{owner_id}/{persona_id}/{relative}``
— the workspace-relative path); the two sidecar suffixes (``.f5.json`` and
``.meta.json``) route through it as SIBLING keys so artifact-listing works on S3.

- ``LocalFileStorage`` (**DEFAULT**) resolves the key under the workspace root
  with the SAME ``resolve_sandbox_path`` + ``O_NOFOLLOW`` primitives the code
  used inline — so community / single-node output is **byte-identical** and the
  path-traversal / TOCTOU protections are unchanged.
- ``S3FileStorage`` puts/gets objects via **boto3** (generic S3 — Tigris / R2 /
  MinIO / AWS). ``boto3`` is an OPTIONAL dependency: the import is guarded so the
  local/community path pulls nothing new at runtime; only constructing the S3
  backend requires it.

Selected by ``PERSONA_API_STORAGE_BACKEND ∈ {local, s3}`` at
``build_file_storage`` (mirrors ``_build_rate_limiter`` / the audit factory).
The file-tool sandbox (``PERSONA_TOOLS_SANDBOX_ROOT``) is DEFERRED (R5-D-4) — a
POSIX-path sandbox the runtime reads/writes mid-turn; v1 covers API-produced
artifacts / uploads / images.
"""

from __future__ import annotations

import mimetypes
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from persona.logging import get_logger
from persona.tools._sandbox import (
    is_regular_file_nofollow,
    open_nofollow,
    read_nofollow_bytes,
    resolve_sandbox_path,
    write_nofollow_bytes,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from persona_api.config import APIConfig

_log = get_logger("api.storage")

__all__ = [
    "FileStorage",
    "LocalFileStorage",
    "S3FileStorage",
    "StorageObject",
    "build_file_storage",
]

_STREAM_CHUNK = 1 << 20  # 1 MiB — matches read_nofollow_bytes' read granularity.


@dataclass(frozen=True, slots=True)
class StorageObject:
    """One object under a ``list`` prefix: its logical ``key`` + byte ``size``.

    ``key`` is the store-relative logical key (forward-slashed), so the artifact
    endpoint derives its ``ref`` by stripping the ``{owner}/{persona}/`` prefix —
    identical to the old ``path.relative_to(persona_root)``. ``size`` avoids a
    second stat/head per object.
    """

    key: str
    size: int


@runtime_checkable
class FileStorage(Protocol):
    """A content store keyed by a logical key (R5-D-4).

    Implementations must be safe to share across requests/threads. ``put`` is
    idempotent-overwrite (re-storing the same key replaces it); it returns the
    key as a ref. Sidecars are stored as ordinary sibling keys
    (``<key>.f5.json`` / ``<key>.meta.json``).
    """

    def put(self, key: str, data: bytes, *, content_type: str | None = None) -> str:
        """Store ``data`` at ``key``; return ``key`` as the ref."""
        ...

    def get(self, key: str) -> bytes:
        """Read all bytes at ``key`` (raises if absent)."""
        ...

    def open_stream(self, key: str) -> Iterator[bytes]:
        """Yield the object's bytes in chunks (for a streaming serve response)."""
        ...

    def exists(self, key: str) -> bool:
        """Whether a regular object exists at ``key``."""
        ...

    def delete(self, key: str) -> bool:
        """Delete ``key``; return True if it existed."""
        ...

    def list(self, prefix: str) -> Iterator[StorageObject]:
        """Yield every object whose key is under ``prefix`` (recursive).

        Includes sidecars — the caller filters them (``is_any_sidecar``), exactly
        as the pre-R5 walk did. Keys are store-relative + forward-slashed.
        """
        ...


class LocalFileStorage:
    """Filesystem-backed storage under a root (the workspace root) — the DEFAULT.

    Byte-identical to the pre-R5 inline writes: the key is resolved under the root
    via ``resolve_sandbox_path`` (rejecting traversal / absolute / NUL) and the
    bytes are written via the shared ``O_NOFOLLOW`` opener (TOCTOU-safe). Sidecars
    are just keys with a ``.f5.json`` / ``.meta.json`` suffix — sibling files, as
    before. ``content_type`` is ignored (the extension carries type on a POSIX FS).
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    def _resolve(self, key: str) -> Path:
        return resolve_sandbox_path(self._root, key)

    def put(self, key: str, data: bytes, *, content_type: str | None = None) -> str:
        _ = content_type  # POSIX FS carries type via the extension; unused here.
        resolved = self._resolve(key)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        write_nofollow_bytes(resolved, data)
        return key

    def get(self, key: str) -> bytes:
        return read_nofollow_bytes(self._resolve(key))

    def open_stream(self, key: str) -> Iterator[bytes]:
        return _iter_file_nofollow(self._resolve(key))

    def exists(self, key: str) -> bool:
        return is_regular_file_nofollow(self._resolve(key))

    def delete(self, key: str) -> bool:
        resolved = self._resolve(key)
        if not is_regular_file_nofollow(resolved):
            return False
        resolved.unlink()
        return True

    def list(self, prefix: str) -> Iterator[StorageObject]:
        # Behavior-identical to the pre-R5 artifact walk: ``rglob('*')`` +
        # ``is_file()`` (the endpoint's exact check), yielding store-relative,
        # forward-slashed keys. A missing prefix dir yields nothing.
        base = self._resolve(prefix)
        if not base.is_dir():
            return
        stem = prefix.rstrip("/")
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            # Key = ``{prefix}/{path-relative-to-base}`` — computed against ``base``
            # (the resolved prefix dir) so the store-root symlink canonicalisation
            # (/tmp → /private/tmp) never breaks ``relative_to``.
            key = f"{stem}/{path.relative_to(base).as_posix()}"
            yield StorageObject(key=key, size=path.stat().st_size)


def _iter_file_nofollow(path: Path) -> Iterator[bytes]:
    """Stream a file's bytes in chunks via the O_NOFOLLOW opener (serve path)."""
    fd = open_nofollow(path, os.O_RDONLY)
    try:
        while True:
            chunk = os.read(fd, _STREAM_CHUNK)
            if not chunk:
                break
            yield chunk
    finally:
        os.close(fd)


class S3FileStorage:
    """Generic S3-compatible storage (Tigris / R2 / MinIO / AWS) via boto3.

    ``boto3`` is an OPTIONAL dependency — imported HERE (guarded), so the
    local/community path never needs it. Credentials come from the standard
    ``AWS_*`` env (``fly storage create`` provisions them across all instances for
    Tigris); ``endpoint_url`` + ``region`` are config. Sidecars are ordinary
    sibling object keys, so the artifact-list walk works on S3 too.
    """

    def __init__(
        self,
        bucket: str,
        *,
        endpoint_url: str | None = None,
        region: str | None = None,
    ) -> None:
        try:
            import boto3  # noqa: PLC0415 — optional dep, guarded so local pulls nothing
        except ImportError as exc:  # pragma: no cover - exercised only without boto3
            msg = (
                "PERSONA_API_STORAGE_BACKEND=s3 requires the optional 'boto3' dependency; "
                "install it (e.g. `uv pip install boto3`) or use the local backend."
            )
            raise RuntimeError(msg) from exc
        self._bucket = bucket
        self._client = boto3.client("s3", endpoint_url=endpoint_url, region_name=region)

    def put(self, key: str, data: bytes, *, content_type: str | None = None) -> str:
        ctype = content_type or mimetypes.guess_type(key)[0] or "application/octet-stream"
        self._client.put_object(Bucket=self._bucket, Key=key, Body=data, ContentType=ctype)
        return key

    def get(self, key: str) -> bytes:
        resp = self._client.get_object(Bucket=self._bucket, Key=key)
        body: bytes = resp["Body"].read()
        return body

    def open_stream(self, key: str) -> Iterator[bytes]:
        resp = self._client.get_object(Bucket=self._bucket, Key=key)
        yield from resp["Body"].iter_chunks(chunk_size=_STREAM_CHUNK)

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError  # noqa: PLC0415 — optional dep, guarded

        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise
        return True

    def delete(self, key: str) -> bool:
        existed = self.exists(key)
        if existed:
            self._client.delete_object(Bucket=self._bucket, Key=key)
        return existed

    def list(self, prefix: str) -> Iterator[StorageObject]:
        # list_objects_v2 paginator: handles >1000-object buckets transparently.
        # Normalise the prefix to a folder boundary so ``a/b`` doesn't also match
        # ``a/bc``; keys come back store-absolute (the whole point on S3).
        normalized = prefix if prefix.endswith("/") else prefix + "/"
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._bucket, Prefix=normalized):
            for obj in page.get("Contents", []):
                yield StorageObject(key=obj["Key"], size=int(obj["Size"]))


def build_file_storage(config: APIConfig, workspace_root: Path) -> FileStorage:
    """Select the storage backend (R5-D-4): S3 when configured, else local.

    Falls back to local when ``s3`` is requested but no bucket is set — the same
    graceful shape as ``_build_rate_limiter`` / the audit factory (a misconfigured
    S3 backend must not brick a single-node boot)."""
    if config.storage_backend == "s3" and config.storage_s3_bucket:
        _log.info("file storage: S3 backend bucket={bucket}", bucket=config.storage_s3_bucket)
        return S3FileStorage(
            config.storage_s3_bucket,
            endpoint_url=config.storage_s3_endpoint_url or None,
            region=config.storage_s3_region or None,
        )
    return LocalFileStorage(workspace_root)
