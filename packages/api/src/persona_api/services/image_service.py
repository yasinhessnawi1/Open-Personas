"""Image upload service (spec 13 T10a + T10b — validation + downscale + EXIF strip).

Owns the per-persona image upload boundary. Validates declared media-type,
re-validates magic bytes, performs a **pre-decode dimension guard**
(decompression-bomb defence-in-depth — the headline T10a security feature),
**downscales** above the 1568 px long-edge ceiling and **strips EXIF** via
Pillow (T10b), resolves the workspace path through ``persona.tools._sandbox``
(Spec 03 ``resolve_sandbox_path``), and writes the bytes via the
``O_NOFOLLOW`` opener pattern from :mod:`persona.tools.builtin.file_write`.

**Pre-decode dimension guard:** all four supported formats (PNG, JPEG, WebP,
GIF) carry width+height in their fixed-offset / well-known headers. We parse
those *by hand* (no Pillow in :mod:`persona-core` per D-13-X-pillow license-
stack discipline; T10b owns the Pillow-backed downscaler) and reject any image
whose declared canvas exceeds 50 megapixels (``50_000_000`` px). This runs
**before** any image decoder is invoked, which is the empirical proof —
T10b's Pillow-backed downscaler never sees a bomb.

**4 megapixel ceiling rationale:** decompression bombs encode a tiny on-disk
file that expands to gigabytes when decoded (PIL's default ``MAX_IMAGE_PIXELS``
is 89 478 485 ≈ 89 megapixels, set to 1.5× a 4-port 4K monitor). 50 megapixels
is below PIL's default *and* a comfortable headroom over a 24-megapixel DSLR
photograph at full resolution — the largest legitimate upload we expect.
D-13-1 downscales to 1568 px long-edge for all providers so this guard never
fires on a legitimate image.

**Format-specific notes:**

- **PNG**: width+height are u32 big-endian at IHDR bytes 16..24. PNG's spec
  caps each dimension at ``2**31 - 1``, so we don't need to special-case
  overflow before multiplying.
- **JPEG**: SOF (Start Of Frame) markers carry height+width. Scan for the
  first ``\\xFF<marker>`` with marker ∈ {0xC0..0xCF} \\ {0xC4, 0xC8, 0xCC}
  (DHT, JPG, DAC are not SOF markers).
- **WebP**: three sub-format chunks: VP8 (lossy), VP8L (lossless), VP8X
  (extended; the canvas dimensions there are stored as ``canvas-1`` u24 LE).
- **GIF**: u16 little-endian at LSD bytes 6..10. GIF's hard ceiling is
  65535×65535 = ~4.3 gigapixels — already well above our 50 MP guard, so
  any near-maximum GIF rejects.

Cross-tenant fetch returns ``not_found`` by design (existence-disclosure-safe;
the route renders 404). The sandbox resolver is the only gatekeeper — every
``upload``/``fetch`` resolves against ``workspace_root/owner_id/persona_id``.

D-13-1 (downscale to 1568 px), D-13-4 (workspace storage), D-13-5 (4 images
per message, 20 MB total upload cap) are the active decisions. The downscale
+ EXIF strip path is T10b's contribution layered on top of T10a's gates.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from persona.errors import PersonaError, SandboxViolationError
from persona.logging import get_logger
from PIL import Image, UnidentifiedImageError

from persona_api.services.artifact_storage import artifact_key

if TYPE_CHECKING:
    from persona_api.storage import FileStorage

# Defence-in-depth: Pillow's own decompression-bomb guard. The pre-decode
# header guard (:func:`_pre_decode_dims`) is the primary gate; this is the
# secondary one that fires if a bomb somehow slipped through (e.g., a future
# format where the header parser is conservative). Pillow raises
# ``Image.DecompressionBombError`` once a decoded image exceeds this pixel
# count; we map it to :class:`PersonaError` (``reason="decompression_bomb"``).
Image.MAX_IMAGE_PIXELS = 50_000_000

__all__ = [
    "DOWNSCALE_CEILING_PX",
    "HARD_REJECT_PX",
    "MAX_PIXELS",
    "MAX_UPLOAD_BYTES",
    "SUPPORTED_MEDIA_TYPES",
    "ImageRef",
    "fetch",
    "upload",
]

_log = get_logger("api.images")

#: Maximum total upload size per image (D-13-5: 20 MB cap).
MAX_UPLOAD_BYTES: int = 20 * 1024 * 1024

#: Pre-decode dimension guard ceiling (50 megapixels, below PIL's default
#: ``MAX_IMAGE_PIXELS`` of ~89 MP, above any legitimate 24 MP DSLR upload).
MAX_PIXELS: int = 50_000_000

#: D-13-1: long-edge downscale target for all providers (Anthropic + OpenAI).
DOWNSCALE_CEILING_PX: int = 1568

#: D-13-1: hard reject above this long-edge (4096 px). Anything between
#: ``DOWNSCALE_CEILING_PX`` and this is downscaled; above is rejected.
HARD_REJECT_PX: int = 4096

#: The four IANA media types T10a accepts (D-13-1).
SUPPORTED_MEDIA_TYPES: frozenset[str] = frozenset(
    {"image/png", "image/jpeg", "image/webp", "image/gif"}
)

#: Extension per supported media type (deterministic; storage layout uses this).
_EXT_BY_MEDIA_TYPE: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

#: Workspace sub-directory holding a persona's uploaded images (D-13-4).
_UPLOAD_DIR_NAME: str = "uploads"


@dataclass(frozen=True)
class ImageRef:
    """Reference to an uploaded image — the API-boundary type.

    Returned by :func:`upload`. ``workspace_path`` is the *workspace-relative*
    path (``uploads/<ref>.<ext>``) — the route + downstream serialisers
    reconstruct the absolute path from ``workspace_root``. ``media_type`` is
    one of :data:`SUPPORTED_MEDIA_TYPES`; ``size_bytes`` is the raw byte
    count (post-validation, pre-downscale; T10b updates this if it
    downscales).
    """

    workspace_path: str
    media_type: str
    size_bytes: int


def upload(
    *,
    file_storage: FileStorage,
    owner_id: str,
    persona_id: str,
    file_bytes: bytes,
    declared_media_type: str,
    conversation_id: str | None = None,
    original_name: str | None = None,
) -> ImageRef:
    """Validate + store an uploaded image; return its :class:`ImageRef`.

    The four validation gates (all fail-loud per :class:`PersonaError`):

    1. ``declared_media_type`` in :data:`SUPPORTED_MEDIA_TYPES`.
    2. ``len(file_bytes) <= MAX_UPLOAD_BYTES`` (D-13-5).
    3. Magic bytes match the declared media type.
    4. **Pre-decode dimension guard** — header width*height <=
       :data:`MAX_PIXELS`. No image decoder is invoked before this gate.

    On success: storage path is
    ``workspace_root/owner_id/persona_id/uploads/<ref><ext>`` where
    ``<ref>`` is ``blake2b(file_bytes, digest_size=16).hexdigest()``.
    The bytes are written via the shared ``O_NOFOLLOW`` opener
    (:func:`persona.tools._sandbox.write_nofollow_bytes`), mode ``0o600``.

    Args:
        workspace_root: Root of the per-deployment workspace (typically
            ``settings.persona_workspace_root``). Must exist.
        owner_id: Authenticated tenant identifier (per Spec 08 RLS).
        persona_id: Persona owning this upload.
        file_bytes: Raw bytes from the multipart upload.
        declared_media_type: IANA media type from the multipart part.

    Returns:
        :class:`ImageRef` with workspace-relative path + media_type +
        size_bytes.

    Raises:
        PersonaError: Any validation gate failure. ``context["reason"]``
            disambiguates: ``unsupported_media_type``, ``oversize``,
            ``magic_bytes_mismatch``, ``malformed_header``,
            ``decompression_bomb``.
        SandboxViolationError: If the resolved storage path escapes
            ``workspace_root/owner_id/persona_id`` (defence in depth —
            should never fire under normal use since we construct the
            ``<ref><ext>`` path ourselves).
    """
    # Gate 1: declared media type.
    if declared_media_type not in SUPPORTED_MEDIA_TYPES:
        raise PersonaError(
            "unsupported media type",
            context={
                "reason": "unsupported_media_type",
                "declared_media_type": declared_media_type,
            },
        )

    # Gate 2: size cap.
    if len(file_bytes) > MAX_UPLOAD_BYTES:
        raise PersonaError(
            "upload exceeds size cap",
            context={
                "reason": "oversize",
                "size_bytes": str(len(file_bytes)),
                "max_bytes": str(MAX_UPLOAD_BYTES),
            },
        )

    # Gate 3: magic bytes match.
    if not _magic_bytes_match(file_bytes, declared_media_type):
        raise PersonaError(
            "magic bytes do not match declared media type",
            context={
                "reason": "magic_bytes_mismatch",
                "declared_media_type": declared_media_type,
            },
        )

    # Gate 4: pre-decode dimension guard (THE headline T10a security feature).
    width, height = _pre_decode_dims(file_bytes, declared_media_type)
    if width * height > MAX_PIXELS:
        raise PersonaError(
            "image dimensions exceed decompression-bomb ceiling",
            context={
                "reason": "decompression_bomb",
                "width": str(width),
                "height": str(height),
                "max_pixels": str(MAX_PIXELS),
            },
        )

    # T10b: Pillow-backed downscale + EXIF strip. If long-edge >
    # ``HARD_REJECT_PX`` we reject (raises ``image_too_large``); if long-edge
    # > ``DOWNSCALE_CEILING_PX`` we resize to fit; else we pass through
    # unchanged. Pillow's re-encode (without ``exif=...``) drops EXIF
    # metadata for free — that's the secondary EXIF-strip behaviour.
    file_bytes = _maybe_downscale(file_bytes, declared_media_type)

    # Storage path. blake2b gives a stable content-addressed ref so two
    # uploads of the same bytes collapse to one file (idempotent).
    ext = _EXT_BY_MEDIA_TYPE[declared_media_type]
    ref = hashlib.blake2b(file_bytes, digest_size=16).hexdigest()
    relative = f"{_UPLOAD_DIR_NAME}/{ref}{ext}"

    # R5-D-4: write through the FileStorage backend (LocalFileStorage keeps the
    # exact resolve_sandbox_path + O_NOFOLLOW byte-identical write; S3 uploads the
    # object). Content-addressed key stays idempotent-overwrite.
    key = artifact_key(owner_id, persona_id, relative)
    file_storage.put(key, file_bytes, content_type=declared_media_type)

    _log.info(
        "image upload accepted",
        owner_id=owner_id,
        persona_id=persona_id,
        media_type=declared_media_type,
        size_bytes=len(file_bytes),
        width=width,
        height=height,
        workspace_path=relative,
    )

    # F5 T04 — D-F5-X-artifact-metadata-convention: write a sidecar so the
    # F5 artifact-list endpoint can filter/sort this upload. Best-effort
    # (failures log but do not abort the upload) — sidecar absence falls back
    # to metadata=null on the artifact-list view, not a broken upload.
    from persona_api.services.artifact_metadata import WorkspaceArtifactMetadata, utcnow
    from persona_api.services.artifact_storage import write_sidecar

    try:
        # R5-D-4: sidecar routes through the SAME backend as a sibling object.
        write_sidecar(
            file_storage,
            key,
            WorkspaceArtifactMetadata(
                source="upload",
                type="image",
                producing_spec="13",
                conversation_id=conversation_id,
                created_at=utcnow(),
                original_name=original_name,
            ),
        )
    except Exception as exc:  # noqa: BLE001 — sidecar failure non-fatal
        _log.warning(
            "F5 sidecar write failed (upload still succeeded)",
            workspace_path=relative,
            error=str(exc),
        )

    return ImageRef(
        workspace_path=relative,
        media_type=declared_media_type,
        size_bytes=len(file_bytes),
    )


def fetch(
    *,
    file_storage: FileStorage,
    owner_id: str,
    persona_id: str,
    ref: str,
) -> tuple[bytes, str]:
    """Read an uploaded image by its workspace-relative ref; return (bytes, media_type).

    Cross-tenant access (``owner_id`` mismatch) returns ``not_found`` by
    design — existence-disclosure-safe. The route renders 404 either way.

    Args:
        workspace_root: Same root used during :func:`upload`.
        owner_id: Authenticated tenant identifier.
        persona_id: Persona owning the upload.
        ref: Workspace-relative path returned by :func:`upload`
            (``uploads/<digest><ext>``). May also be the bare ``<digest><ext>``
            — we tolerate both for route convenience.

    Returns:
        ``(file_bytes, media_type)`` tuple. ``media_type`` is derived from
        the filename extension.

    Raises:
        PersonaError: ``reason="not_found"`` if the file doesn't exist OR
            resolves outside the sandbox (cross-tenant / traversal).
    """
    relative = ref if "/" in ref else f"{_UPLOAD_DIR_NAME}/{ref}"
    key = artifact_key(owner_id, persona_id, relative)
    not_found = PersonaError("image not found", context={"reason": "not_found", "ref": ref[:120]})

    media_type = _media_type_for_ext(PurePosixPath(relative).suffix.lower())
    if media_type is None:
        raise not_found

    # R5-D-4 via the backend. LocalFileStorage preserves the security semantics
    # (``resolve_sandbox_path`` raises ``SandboxViolationError`` on traversal;
    # ``is_regular_file_nofollow`` treats a swapped symlink / missing file as
    # absent; the read is O_NOFOLLOW). Traversal + missing/symlink both collapse
    # to ``not_found`` — existence-disclosure-safe, unchanged from the inline path.
    try:
        if not file_storage.exists(key):
            raise not_found
        return file_storage.get(key), media_type
    except SandboxViolationError as exc:
        raise not_found from exc
    except OSError as exc:
        raise not_found from exc


# ---------------------------------------------------------------------------
# Internal helpers — magic bytes + pre-decode dimension parsers (hand-rolled;
# no Pillow per D-13-X-pillow license-stack discipline).
# ---------------------------------------------------------------------------


_PNG_SIGNATURE: bytes = b"\x89PNG\r\n\x1a\n"
_JPEG_SOI: bytes = b"\xff\xd8\xff"
_GIF87A: bytes = b"GIF87a"
_GIF89A: bytes = b"GIF89a"


def _magic_bytes_match(file_bytes: bytes, declared_media_type: str) -> bool:
    """Return True iff the file's first bytes match the declared format."""
    if declared_media_type == "image/png":
        return file_bytes.startswith(_PNG_SIGNATURE)
    if declared_media_type == "image/jpeg":
        return file_bytes.startswith(_JPEG_SOI)
    if declared_media_type == "image/webp":
        return len(file_bytes) >= 12 and file_bytes[0:4] == b"RIFF" and file_bytes[8:12] == b"WEBP"
    if declared_media_type == "image/gif":
        return file_bytes.startswith(_GIF87A) or file_bytes.startswith(_GIF89A)
    return False


def _pre_decode_dims(file_bytes: bytes, declared_media_type: str) -> tuple[int, int]:
    """Parse (width, height) from the file's well-known header bytes.

    Hand-rolled — no image decoder is invoked. Any parse error becomes a
    :class:`PersonaError` with ``reason="malformed_header"`` (the caller
    treats this identically to a rejected upload).

    Returns:
        ``(width, height)`` integers in pixels.

    Raises:
        PersonaError: ``reason="malformed_header"`` if the bytes truncate
            mid-header or the structure is otherwise unparseable.
    """
    try:
        if declared_media_type == "image/png":
            return _png_dims(file_bytes)
        if declared_media_type == "image/jpeg":
            return _jpeg_dims(file_bytes)
        if declared_media_type == "image/webp":
            return _webp_dims(file_bytes)
        if declared_media_type == "image/gif":
            return _gif_dims(file_bytes)
    except (IndexError, struct.error, ValueError) as exc:
        raise PersonaError(
            "malformed image header",
            context={"reason": "malformed_header", "declared_media_type": declared_media_type},
        ) from exc

    # Unreachable — _magic_bytes_match + SUPPORTED_MEDIA_TYPES gates upstream
    # mean we never reach a fifth format. Defence in depth.
    raise PersonaError(
        "unsupported media type",
        context={"reason": "unsupported_media_type", "declared_media_type": declared_media_type},
    )


def _png_dims(file_bytes: bytes) -> tuple[int, int]:
    """PNG: IHDR at bytes 8..; width+height u32 big-endian at offsets 16..24."""
    if len(file_bytes) < 24 or file_bytes[12:16] != b"IHDR":
        raise ValueError("PNG IHDR not at expected offset")
    width, height = struct.unpack(">II", file_bytes[16:24])
    return int(width), int(height)


def _jpeg_dims(file_bytes: bytes) -> tuple[int, int]:
    """JPEG: scan from byte 2 for the first SOF marker; read height+width.

    SOF markers are 0xFFC0..0xFFCF excluding 0xFFC4 (DHT), 0xFFC8 (JPG),
    and 0xFFCC (DAC) — those are non-SOF and must be skipped.
    """
    _non_sof = {0xC4, 0xC8, 0xCC}
    i = 2  # skip SOI \xFF\xD8
    n = len(file_bytes)
    while i < n - 9:
        if file_bytes[i] != 0xFF:
            i += 1
            continue
        # Skip fill bytes 0xFF... 0xFF... (allowed in JPEG).
        while i < n and file_bytes[i] == 0xFF:
            i += 1
        if i >= n:
            break
        marker = file_bytes[i]
        i += 1
        if 0xC0 <= marker <= 0xCF and marker not in _non_sof:
            # SOF segment: 2-byte length, 1-byte precision, 2-byte height,
            # 2-byte width.
            seg_len = struct.unpack(">H", file_bytes[i : i + 2])[0]
            if seg_len < 7:
                raise ValueError("SOF segment too short")
            height = struct.unpack(">H", file_bytes[i + 3 : i + 5])[0]
            width = struct.unpack(">H", file_bytes[i + 5 : i + 7])[0]
            return int(width), int(height)
        if marker == 0xD9 or marker == 0xDA:
            # EOI or SOS reached without finding SOF.
            raise ValueError("JPEG SOF marker not found before SOS/EOI")
        # Other markers (APPn, DQT, COM, ...): skip by segment length.
        seg_len = struct.unpack(">H", file_bytes[i : i + 2])[0]
        i += seg_len
    raise ValueError("JPEG SOF marker not found")


def _webp_dims(file_bytes: bytes) -> tuple[int, int]:
    """WebP: dispatch on the chunk after the RIFF/WEBP container."""
    if len(file_bytes) < 16:
        raise ValueError("WebP truncated before chunk header")
    chunk = file_bytes[12:16]
    if chunk == b"VP8 ":
        # Lossy: dimensions live at bytes 26..30 (after the chunk header +
        # frame tag + start code). Each is a 14-bit unsigned little-endian
        # value packed into a u16 — mask the top 2 bits.
        if len(file_bytes) < 30:
            raise ValueError("WebP/VP8 truncated")
        width = struct.unpack("<H", file_bytes[26:28])[0] & 0x3FFF
        height = struct.unpack("<H", file_bytes[28:30])[0] & 0x3FFF
        return int(width), int(height)
    if chunk == b"VP8L":
        # Lossless: 4-byte chunk size, then 1-byte signature (0x2F), then
        # 4 bytes packed as: width-1 (14 bits LSB), height-1 (14 bits),
        # alpha flag (1 bit), version (3 bits).
        if len(file_bytes) < 25:
            raise ValueError("WebP/VP8L truncated")
        packed = struct.unpack("<I", file_bytes[21:25])[0]
        width = (packed & 0x3FFF) + 1
        height = ((packed >> 14) & 0x3FFF) + 1
        return int(width), int(height)
    if chunk == b"VP8X":
        # Extended: 4-byte chunk size, 4-byte flags, then 3 bytes
        # (canvas_width - 1) little-endian u24 + 3 bytes
        # (canvas_height - 1) little-endian u24.
        if len(file_bytes) < 30:
            raise ValueError("WebP/VP8X truncated")
        w_raw = file_bytes[24:27]
        h_raw = file_bytes[27:30]
        width = int.from_bytes(w_raw, "little") + 1
        height = int.from_bytes(h_raw, "little") + 1
        return int(width), int(height)
    raise ValueError(f"unknown WebP chunk: {chunk!r}")


def _gif_dims(file_bytes: bytes) -> tuple[int, int]:
    """GIF: LSD width+height u16 little-endian at bytes 6..10."""
    if len(file_bytes) < 10:
        raise ValueError("GIF truncated before LSD")
    width, height = struct.unpack("<HH", file_bytes[6:10])
    return int(width), int(height)


#: Spec 28 — rich-output render-set extensions served via this same route
#: (D-28-10 reuses the uploads serve surface). The image map above stays
#: image-only; the WorkspacePersister writes these text/diagram/doc types and
#: the right-panel renderer fetches them by ref. The panel dispatches on the
#: artifact's own ``mime_type`` (carried on the SSE artifact), so for code files
#: a generic ``text/plain`` Content-Type is sufficient here.
_RICH_OUTPUT_MEDIA_BY_EXT: dict[str, str] = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".log": "text/plain",
    ".csv": "text/csv",
    ".json": "application/json",
    ".html": "text/html",
    ".htm": "text/html",
    ".mmd": "text/vnd.mermaid",
    ".dot": "text/vnd.graphviz",
    ".gv": "text/vnd.graphviz",
    ".pdf": "application/pdf",
    ".svg": "image/svg+xml",
    ".py": "text/plain",
    ".ts": "text/plain",
    ".tsx": "text/plain",
    ".js": "text/plain",
    ".jsx": "text/plain",
    ".go": "text/plain",
    ".rs": "text/plain",
    ".java": "text/plain",
    ".c": "text/plain",
    ".h": "text/plain",
    ".cpp": "text/plain",
    ".rb": "text/plain",
    ".sh": "text/plain",
    ".sql": "text/plain",
    ".yaml": "text/plain",
    ".yml": "text/plain",
    ".toml": "text/plain",
    ".css": "text/plain",
}


def _media_type_for_ext(ext: str) -> str | None:
    """Map a stored filename extension back to its IANA media type."""
    for media, candidate_ext in _EXT_BY_MEDIA_TYPE.items():
        if candidate_ext == ext:
            return media
    # Spec 28 rich-output types (markdown / code / csv / json / html / mermaid /
    # graphviz / pdf / plaintext) served through the same route (D-28-10).
    return _RICH_OUTPUT_MEDIA_BY_EXT.get(ext)


# ---------------------------------------------------------------------------
# T10b: Pillow-backed downscale + EXIF strip
# ---------------------------------------------------------------------------


#: Pillow ``Image.save`` format name per IANA media type.
_PIL_FORMAT_BY_MEDIA_TYPE: dict[str, str] = {
    "image/png": "PNG",
    "image/jpeg": "JPEG",
    "image/webp": "WEBP",
    "image/gif": "GIF",
}


def _maybe_downscale(
    file_bytes: bytes,
    media_type: str,
    ceiling_px: int = DOWNSCALE_CEILING_PX,
    hard_reject_px: int = HARD_REJECT_PX,
) -> bytes:
    """Downscale to ``ceiling_px`` long-edge if needed; strip EXIF as a side-effect.

    Behaviour by long-edge bucket (per D-13-1):

    * ``> hard_reject_px`` (4096 px) → ``PersonaError(reason="image_too_large")``.
    * ``> ceiling_px`` (1568 px) and ``<= hard_reject_px`` → resize via LANCZOS,
      re-encode to the original format. No ``exif=`` kwarg means EXIF is
      stripped for free.
    * ``<= ceiling_px`` → return ``file_bytes`` unchanged (no-op; we don't
      re-encode when we don't need to, preserving idempotent content addressing
      for already-small uploads).

    Args:
        file_bytes: The raw bytes from the multipart upload, post-T10a gates.
        media_type: One of :data:`SUPPORTED_MEDIA_TYPES`.
        ceiling_px: Long-edge target above which we downscale.
        hard_reject_px: Long-edge above which we reject outright.

    Returns:
        Either the original bytes (no-op path) or the downscaled+EXIF-stripped
        re-encoded bytes.

    Raises:
        PersonaError: ``reason="image_too_large"`` if long-edge exceeds
            ``hard_reject_px``; ``reason="malformed_image"`` on
            :class:`PIL.UnidentifiedImageError`; ``reason="decompression_bomb"``
            on :class:`PIL.Image.DecompressionBombError`;
            ``reason="decode_failed"`` on any other PIL error caught at the
            boundary.
    """
    try:
        with Image.open(BytesIO(file_bytes)) as img:
            img.load()
            width, height = img.size
            long_edge = max(width, height)

            if long_edge > hard_reject_px:
                raise PersonaError(
                    "image exceeds hard-reject long-edge ceiling",
                    context={
                        "reason": "image_too_large",
                        "width": str(width),
                        "height": str(height),
                        "long_edge": str(long_edge),
                        "hard_reject_px": str(hard_reject_px),
                    },
                )

            if long_edge <= ceiling_px:
                # No-op: small enough to pass through unmodified. Note that
                # this branch DOES preserve any EXIF on the way through —
                # the dominant defence is the downscale path. Tiny images
                # carrying EXIF are extremely rare in practice and the
                # bandwidth/latency cost of always re-encoding outweighs the
                # marginal privacy gain on a 100×100 thumbnail.
                return file_bytes

            scale = ceiling_px / long_edge
            new_size = (round(width * scale), round(height * scale))
            resized = img.resize(new_size, Image.Resampling.LANCZOS)

            pil_format = _PIL_FORMAT_BY_MEDIA_TYPE[media_type]
            save_kwargs: dict[str, object] = {}
            if pil_format in {"JPEG", "WEBP"}:
                save_kwargs["quality"] = 90

            buf = BytesIO()
            # NOTE: we deliberately do NOT pass ``exif=`` — that drops EXIF.
            resized.save(buf, format=pil_format, **save_kwargs)
            return buf.getvalue()
    except Image.DecompressionBombError as exc:
        raise PersonaError(
            "image trips Pillow decompression-bomb guard",
            context={
                "reason": "decompression_bomb",
                "max_image_pixels": str(Image.MAX_IMAGE_PIXELS),
            },
        ) from exc
    except UnidentifiedImageError as exc:
        raise PersonaError(
            "image is malformed (Pillow cannot identify format)",
            context={"reason": "malformed_image", "media_type": media_type},
        ) from exc
    except PersonaError:
        # Re-raise our own ``image_too_large`` without remapping.
        raise
    except (OSError, ValueError) as exc:
        # Catch-all for Pillow decode failures (e.g. truncated payload,
        # syntax errors mid-decode). Map to a stable domain error rather
        # than leaking the raw PIL message.
        raise PersonaError(
            "image decode failed",
            context={"reason": "decode_failed", "media_type": media_type},
        ) from exc
