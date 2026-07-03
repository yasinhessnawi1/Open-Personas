"""Unit tests for ``persona_api.services.image_service`` (spec 13 T10a + T10b).

Covers the four validation gates (unsupported media type, oversize, magic-
bytes mismatch, decompression-bomb pre-decode dimension guard), the happy-
path upload + fetch roundtrip, the cross-tenant existence-disclosure-safe
fetch, the T10a empirical assertion that NO image decoder is invoked when
the pre-decode guard fires, and the T10b Pillow-backed downscale + EXIF
strip + secondary decompression-bomb guard.

Per D-13-X-pillow ``persona-api`` now depends on Pillow (T10b); the test
file uses Pillow to synthesise the fixtures it needs (3000×3000 downscale,
7000×7000 borderline, 5000×5000 hard-reject, EXIF-bearing JPEG, decoded-
bomb scenarios) — that's allowed under the same dependency.
"""

from __future__ import annotations

import importlib.util
import struct
import zlib
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pytest
from persona.errors import PersonaError
from persona_api.services import image_service
from persona_api.services.image_service import (
    DOWNSCALE_CEILING_PX,
    HARD_REJECT_PX,
    MAX_PIXELS,
    MAX_UPLOAD_BYTES,
    ImageRef,
    _maybe_downscale,
    fetch,
    upload,
)
from persona_api.storage import LocalFileStorage

# Fixture directory (decompression bombs ship as committed files for repeatable
# review — they are tiny and never decoded).
_FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures"


def _minimal_png() -> bytes:
    """Tiny valid 1×1 PNG (RGB) for happy-path tests."""

    def chunk(t: bytes, d: bytes) -> bytes:
        crc = zlib.crc32(t + d) & 0xFFFFFFFF
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", crc)

    ihdr = struct.pack(">II", 1, 1) + bytes([8, 2, 0, 0, 0])
    idat_data = zlib.compress(b"\x00\xff\x00\x00")
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", idat_data)
        + chunk(b"IEND", b"")
    )


def _minimal_jpeg() -> bytes:
    """Tiny valid 1×1 JPEG header — no scan, but the SOF parser only needs SOF0."""
    sof0_payload = (
        bytes([8])  # precision
        + struct.pack(">H", 1)  # height
        + struct.pack(">H", 1)  # width
        + bytes([1])  # num components
        + bytes([1, 0x11, 0])  # one component
    )
    sof0 = b"\xff\xc0" + struct.pack(">H", 2 + len(sof0_payload)) + sof0_payload
    return b"\xff\xd8" + sof0 + b"\xff\xd9"


def _minimal_webp() -> bytes:
    """Tiny valid 1×1 WebP/VP8X header."""
    vp8x_payload = (
        b"\x00\x00\x00\x00"  # flags
        + (0).to_bytes(3, "little")  # canvas_w - 1 = 0 (width=1)
        + (0).to_bytes(3, "little")  # canvas_h - 1 = 0 (height=1)
    )
    chunk = b"VP8X" + struct.pack("<I", len(vp8x_payload)) + vp8x_payload
    body = b"WEBP" + chunk
    return b"RIFF" + struct.pack("<I", len(body)) + body


def _minimal_gif() -> bytes:
    """Tiny valid 1×1 GIF89a header."""
    lsd = struct.pack("<HH", 1, 1) + bytes([0, 0, 0])
    return b"GIF89a" + lsd + b"\x3b"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A fresh per-test workspace root."""
    return tmp_path / "workspace"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestUpload:
    def test_happy_path_returns_image_ref(self, workspace: Path) -> None:
        png = _minimal_png()
        ref = upload(
            file_storage=LocalFileStorage(workspace),
            owner_id="owner-1",
            persona_id="persona-A",
            file_bytes=png,
            declared_media_type="image/png",
        )
        assert isinstance(ref, ImageRef)
        assert ref.media_type == "image/png"
        assert ref.size_bytes == len(png)
        assert ref.workspace_path.startswith("uploads/")
        assert ref.workspace_path.endswith(".png")

        # File is on disk inside the per-tenant sandbox.
        on_disk = workspace / "owner-1" / "persona-A" / ref.workspace_path
        assert on_disk.is_file()
        assert on_disk.read_bytes() == png

    def test_idempotent_re_upload(self, workspace: Path) -> None:
        """Same bytes -> same workspace_path (content-addressed)."""
        png = _minimal_png()
        first = upload(
            file_storage=LocalFileStorage(workspace),
            owner_id="o",
            persona_id="p",
            file_bytes=png,
            declared_media_type="image/png",
        )
        second = upload(
            file_storage=LocalFileStorage(workspace),
            owner_id="o",
            persona_id="p",
            file_bytes=png,
            declared_media_type="image/png",
        )
        assert first.workspace_path == second.workspace_path


# ---------------------------------------------------------------------------
# Validation gates (1, 2, 3)
# ---------------------------------------------------------------------------


class TestUploadValidationErrors:
    def test_unsupported_media_type(self, workspace: Path) -> None:
        with pytest.raises(PersonaError) as exc:
            upload(
                file_storage=LocalFileStorage(workspace),
                owner_id="o",
                persona_id="p",
                file_bytes=b"hello",
                declared_media_type="image/bmp",
            )
        assert exc.value.context["reason"] == "unsupported_media_type"

    def test_oversize_rejected(self, workspace: Path) -> None:
        """21 MB > 20 MB D-13-5 cap."""
        oversize = b"\x00" * (MAX_UPLOAD_BYTES + 1)
        with pytest.raises(PersonaError) as exc:
            upload(
                file_storage=LocalFileStorage(workspace),
                owner_id="o",
                persona_id="p",
                file_bytes=oversize,
                declared_media_type="image/png",
            )
        assert exc.value.context["reason"] == "oversize"

    @pytest.mark.parametrize(
        ("declared", "bytes_payload"),
        [
            # Declare PNG, send JPEG SOI.
            ("image/png", b"\xff\xd8\xff" + b"\x00" * 32),
            # Declare JPEG, send PNG signature.
            ("image/jpeg", b"\x89PNG\r\n\x1a\n" + b"\x00" * 32),
            # Declare WebP, send GIF89a.
            ("image/webp", b"GIF89a" + b"\x00" * 32),
            # Declare GIF, send arbitrary bytes.
            ("image/gif", b"NOTAGIF" + b"\x00" * 32),
        ],
    )
    def test_magic_bytes_mismatch(
        self, workspace: Path, declared: str, bytes_payload: bytes
    ) -> None:
        with pytest.raises(PersonaError) as exc:
            upload(
                file_storage=LocalFileStorage(workspace),
                owner_id="o",
                persona_id="p",
                file_bytes=bytes_payload,
                declared_media_type=declared,
            )
        assert exc.value.context["reason"] == "magic_bytes_mismatch"


# ---------------------------------------------------------------------------
# Pre-decode dimension guard — the headline T10a security feature
# ---------------------------------------------------------------------------


_BOMB_CASES = [
    ("decompression_bomb.png", "image/png"),
    ("decompression_bomb.jpeg", "image/jpeg"),
    ("decompression_bomb.webp", "image/webp"),
    ("decompression_bomb.gif", "image/gif"),
]


class TestPreDecodeDimensionGuard:
    @pytest.mark.parametrize(("fixture_name", "media_type"), _BOMB_CASES)
    def test_bomb_rejected(self, workspace: Path, fixture_name: str, media_type: str) -> None:
        bomb = (_FIXTURE_DIR / fixture_name).read_bytes()
        with pytest.raises(PersonaError) as exc:
            upload(
                file_storage=LocalFileStorage(workspace),
                owner_id="o",
                persona_id="p",
                file_bytes=bomb,
                declared_media_type=media_type,
            )
        assert exc.value.context["reason"] == "decompression_bomb"
        # The guard reports the parsed width × height — confirm the bomb's
        # declared dims exceed our 50 MP ceiling.
        w = int(exc.value.context["width"])
        h = int(exc.value.context["height"])
        assert w * h > MAX_PIXELS

    @pytest.mark.parametrize(("fixture_name", "media_type"), _BOMB_CASES)
    def test_no_image_decoder_invoked(
        self, workspace: Path, fixture_name: str, media_type: str
    ) -> None:
        """Empirical proof: PIL.Image.open is never called when a bomb is uploaded.

        We mock at the canonical module path. If Pillow is not installed
        (the expected steady-state in ``persona-api`` per D-13-X-pillow),
        the import itself fails — we install a sentinel module so the
        patch resolves, then assert ``call_count == 0`` after the guarded
        upload attempt.
        """
        bomb = (_FIXTURE_DIR / fixture_name).read_bytes()
        # Only patch if PIL is actually importable; otherwise skip the
        # call_count assertion (proof is implicit — we can't even import
        # PIL in this venv).
        if importlib.util.find_spec("PIL") is None:
            # Sanity: just confirm the upload raises without PIL anywhere.
            with pytest.raises(PersonaError) as exc:
                upload(
                    file_storage=LocalFileStorage(workspace),
                    owner_id="o",
                    persona_id="p",
                    file_bytes=bomb,
                    declared_media_type=media_type,
                )
            assert exc.value.context["reason"] == "decompression_bomb"
            return

        with patch("PIL.Image.open") as mock_open:
            with pytest.raises(PersonaError):
                upload(
                    file_storage=LocalFileStorage(workspace),
                    owner_id="o",
                    persona_id="p",
                    file_bytes=bomb,
                    declared_media_type=media_type,
                )
            assert mock_open.call_count == 0


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


class TestFetch:
    def test_roundtrip(self, workspace: Path) -> None:
        png = _minimal_png()
        ref = upload(
            file_storage=LocalFileStorage(workspace),
            owner_id="o",
            persona_id="p",
            file_bytes=png,
            declared_media_type="image/png",
        )
        data, media_type = fetch(
            file_storage=LocalFileStorage(workspace),
            owner_id="o",
            persona_id="p",
            ref=ref.workspace_path,
        )
        assert data == png
        assert media_type == "image/png"

    def test_fetch_by_bare_ref(self, workspace: Path) -> None:
        """``fetch`` tolerates the bare ``<digest><ext>`` (route convenience)."""
        png = _minimal_png()
        ref = upload(
            file_storage=LocalFileStorage(workspace),
            owner_id="o",
            persona_id="p",
            file_bytes=png,
            declared_media_type="image/png",
        )
        bare = ref.workspace_path.removeprefix("uploads/")
        data, media_type = fetch(
            file_storage=LocalFileStorage(workspace),
            owner_id="o",
            persona_id="p",
            ref=bare,
        )
        assert data == png
        assert media_type == "image/png"

    def test_cross_tenant_returns_not_found(self, workspace: Path) -> None:
        png = _minimal_png()
        ref = upload(
            file_storage=LocalFileStorage(workspace),
            owner_id="owner-1",
            persona_id="p",
            file_bytes=png,
            declared_media_type="image/png",
        )
        # Different owner_id — workspace path doesn't exist for that tenant.
        with pytest.raises(PersonaError) as exc:
            fetch(
                file_storage=LocalFileStorage(workspace),
                owner_id="owner-2",
                persona_id="p",
                ref=ref.workspace_path,
            )
        assert exc.value.context["reason"] == "not_found"

    def test_traversal_returns_not_found(self, workspace: Path) -> None:
        # The sandbox resolver rejects "../" traversal; the service maps that
        # to a plain not_found (existence-disclosure-safe).
        with pytest.raises(PersonaError) as exc:
            fetch(
                file_storage=LocalFileStorage(workspace),
                owner_id="o",
                persona_id="p",
                ref="../../../etc/passwd",
            )
        assert exc.value.context["reason"] == "not_found"

    def test_missing_file_returns_not_found(self, workspace: Path) -> None:
        (workspace / "o" / "p" / "uploads").mkdir(parents=True, exist_ok=True)
        with pytest.raises(PersonaError) as exc:
            fetch(
                file_storage=LocalFileStorage(workspace),
                owner_id="o",
                persona_id="p",
                ref="uploads/nonexistent.png",
            )
        assert exc.value.context["reason"] == "not_found"


# ---------------------------------------------------------------------------
# Sandbox traversal at upload time
# ---------------------------------------------------------------------------


class TestUploadSandboxBoundary:
    def test_owner_isolation_on_disk(self, workspace: Path) -> None:
        """Owner A's upload lands under {workspace}/A/{persona}/uploads/..."""
        png = _minimal_png()
        ref = upload(
            file_storage=LocalFileStorage(workspace),
            owner_id="A",
            persona_id="P",
            file_bytes=png,
            declared_media_type="image/png",
        )
        a_path = workspace / "A" / "P" / ref.workspace_path
        b_path = workspace / "B" / "P" / ref.workspace_path
        assert a_path.is_file()
        assert not b_path.exists()


# ---------------------------------------------------------------------------
# T10b: Pillow-backed downscale + EXIF strip
# ---------------------------------------------------------------------------


def _synthesize_png(width: int, height: int) -> bytes:
    """Generate a real PNG of the given dimensions via Pillow.

    Solid-colour image so the on-disk byte count stays small even for
    multi-thousand-pixel canvases.
    """
    from PIL import Image as _PILImage

    buf = BytesIO()
    _PILImage.new("RGB", (width, height), color=(73, 109, 137)).save(buf, format="PNG")
    return buf.getvalue()


def _png_dims_from_bytes(png_bytes: bytes) -> tuple[int, int]:
    """Read width+height from PNG IHDR — no PIL decode, fast inspection."""
    width, height = struct.unpack(">II", png_bytes[16:24])
    return int(width), int(height)


class TestDownscale:
    def test_3000px_png_is_downscaled_to_1568(self, workspace: Path) -> None:
        """A 3000×3000 PNG (over the 1568 ceiling, under the 4096 hard reject) downscales."""
        big = _synthesize_png(3000, 3000)
        ref = upload(
            file_storage=LocalFileStorage(workspace),
            owner_id="o",
            persona_id="p",
            file_bytes=big,
            declared_media_type="image/png",
        )
        # Re-read the stored bytes and confirm long-edge == 1568.
        stored, _ = fetch(
            file_storage=LocalFileStorage(workspace),
            owner_id="o",
            persona_id="p",
            ref=ref.workspace_path,
        )
        w, h = _png_dims_from_bytes(stored)
        assert max(w, h) == DOWNSCALE_CEILING_PX
        # ``ref.size_bytes`` reflects the STORED (post-downscale) byte count.
        assert ref.size_bytes == len(stored)

    def test_small_image_passes_through_unchanged(self, workspace: Path) -> None:
        """A 1×1 PNG (well under the ceiling) stores its original bytes."""
        png = _minimal_png()
        ref = upload(
            file_storage=LocalFileStorage(workspace),
            owner_id="o",
            persona_id="p",
            file_bytes=png,
            declared_media_type="image/png",
        )
        stored, _ = fetch(
            file_storage=LocalFileStorage(workspace),
            owner_id="o",
            persona_id="p",
            ref=ref.workspace_path,
        )
        assert stored == png  # Bit-identical — no re-encode happened.

    def test_borderline_at_hard_reject_ceiling_downscales(self, workspace: Path) -> None:
        """4000×4000 = 16 MP — between the 1568 ceiling and the 4096 hard-reject.

        Proves the downscale path is exercised for canvases just below
        ``HARD_REJECT_PX``. Bytes must round-trip with the long edge at 1568.
        """
        big = _synthesize_png(4000, 4000)
        ref = upload(
            file_storage=LocalFileStorage(workspace),
            owner_id="o",
            persona_id="p",
            file_bytes=big,
            declared_media_type="image/png",
        )
        stored, _ = fetch(
            file_storage=LocalFileStorage(workspace),
            owner_id="o",
            persona_id="p",
            ref=ref.workspace_path,
        )
        w, h = _png_dims_from_bytes(stored)
        assert max(w, h) == DOWNSCALE_CEILING_PX


class TestHardReject:
    def test_5000px_png_is_hard_rejected(self, workspace: Path) -> None:
        """5000×5000 PNG > 4096 px long-edge → ``image_too_large``.

        Pre-decode dim guard at 50 MP does NOT fire (5000² = 25 MP < 50 MP),
        so this exercises ``_maybe_downscale``'s hard-reject branch directly.
        """
        big = _synthesize_png(5000, 5000)
        with pytest.raises(PersonaError) as exc:
            upload(
                file_storage=LocalFileStorage(workspace),
                owner_id="o",
                persona_id="p",
                file_bytes=big,
                declared_media_type="image/png",
            )
        assert exc.value.context["reason"] == "image_too_large"
        assert int(exc.value.context["long_edge"]) == 5000
        assert int(exc.value.context["hard_reject_px"]) == HARD_REJECT_PX


class TestEXIFStrip:
    def test_jpeg_exif_is_stripped_on_downscale(self, workspace: Path) -> None:
        """A 3000×3000 JPEG with EXIF metadata loses the APP1 EXIF marker after upload."""
        from PIL import Image as _PILImage

        # Build a JPEG with synthetic EXIF (GPS-style payload bytes).
        img = _PILImage.new("RGB", (3000, 3000), color=(10, 20, 30))
        # Pillow's ``Image.Exif`` accepts an ``IFD`` we can populate.
        exif = _PILImage.Exif()
        # 0x0110 = Model; 0x010F = Make. Arbitrary strings — Pillow encodes
        # them into the APP1 segment.
        exif[0x010F] = "TestMake"
        exif[0x0110] = "TestModel"
        buf = BytesIO()
        img.save(buf, format="JPEG", exif=exif.tobytes(), quality=90)
        jpeg_with_exif = buf.getvalue()

        # Sanity: the source bytes carry an APP1 EXIF marker (0xFFE1 followed
        # by a 2-byte length then ``Exif\0\0``).
        assert b"Exif\x00\x00" in jpeg_with_exif

        ref = upload(
            file_storage=LocalFileStorage(workspace),
            owner_id="o",
            persona_id="p",
            file_bytes=jpeg_with_exif,
            declared_media_type="image/jpeg",
        )
        stored, _ = fetch(
            file_storage=LocalFileStorage(workspace),
            owner_id="o",
            persona_id="p",
            ref=ref.workspace_path,
        )
        # The downscale path re-encodes; without exif=, Pillow drops the
        # EXIF payload. The APP1 marker may still appear if a fresh empty
        # APP1 is emitted, but the ``Exif\0\0`` identifier must NOT.
        assert b"Exif\x00\x00" not in stored
        # And the make/model strings must not survive.
        assert b"TestMake" not in stored
        assert b"TestModel" not in stored


class TestPillowMaxImagePixelsGuard:
    def test_max_image_pixels_is_50mp(self) -> None:
        """Module-load guarantee: PIL's bomb-guard is clamped to 50 MP."""
        from PIL import Image as _PILImage

        # The service module sets ``Image.MAX_IMAGE_PIXELS = 50_000_000`` at
        # import time as defence-in-depth. Re-import-safe (module-level
        # assignment is idempotent).
        assert _PILImage.MAX_IMAGE_PIXELS == 50_000_000
        # And the service module's own reference matches.
        assert image_service.Image.MAX_IMAGE_PIXELS == 50_000_000

    def test_under_50mp_passes_through_decode(self, workspace: Path) -> None:
        """4000×4000 = 16 MP — DOES decode (under both the pre-decode guard and Pillow's bomb)."""
        valid_big = _synthesize_png(4000, 4000)
        # Reaches upload + _maybe_downscale without DecompressionBombError.
        ref = upload(
            file_storage=LocalFileStorage(workspace),
            owner_id="o",
            persona_id="p",
            file_bytes=valid_big,
            declared_media_type="image/png",
        )
        assert isinstance(ref, ImageRef)

    def test_pillow_bomb_guard_maps_to_persona_error(self) -> None:
        """Force Pillow's DecompressionBombError -> PersonaError(decompression_bomb).

        We can't reach this through ``upload`` because the T10a pre-decode
        header guard fires at 50 MP, and the hard-reject branch in
        ``_maybe_downscale`` fires at 4096 px long-edge — both gates would
        intercept a real bomb before Pillow's own check. To exercise the
        defence-in-depth Pillow layer directly we call ``_maybe_downscale``
        with a relaxed ``hard_reject_px`` so the bytes reach ``Image.load()``.
        Pillow raises ``DecompressionBombError`` once the decoded canvas
        exceeds ``2 × MAX_IMAGE_PIXELS`` (a warning fires between 1× and 2×).
        We synthesise a 12000×12000 = 144 MP PNG (≥ 2× 50 MP) and confirm
        the wrapper maps the error to ``reason="decompression_bomb"``.
        """
        from PIL import Image as _PILImage

        # Temporarily lift Pillow's guard so we can MAKE the file.
        original = _PILImage.MAX_IMAGE_PIXELS
        try:
            _PILImage.MAX_IMAGE_PIXELS = None
            buf = BytesIO()
            _PILImage.new("RGB", (12000, 12000), color=(0, 0, 0)).save(buf, format="PNG")
            big_png = buf.getvalue()
        finally:
            _PILImage.MAX_IMAGE_PIXELS = original

        # Re-impose the 50 MP guard (the module-level default the service set).
        _PILImage.MAX_IMAGE_PIXELS = 50_000_000

        # Bypass the hard-reject branch so we reach Pillow's own bomb guard.
        with pytest.raises(PersonaError) as exc:
            _maybe_downscale(big_png, "image/png", hard_reject_px=100_000)
        assert exc.value.context["reason"] == "decompression_bomb"


class TestMalformedImage:
    def test_malformed_body_maps_to_malformed_image_or_decode_failed(self) -> None:
        """A valid PNG signature with garbage body -> PersonaError (no raw PIL crash).

        Pillow may raise either ``UnidentifiedImageError`` (if the structure
        is malformed enough that it can't even pick a decoder) or an
        ``OSError`` / ``SyntaxError`` during ``.load()``. Both must surface
        as a PersonaError with a stable, domain-level reason.
        """
        garbage = b"\x89PNG\r\n\x1a\n" + b"\xff" * 512
        with pytest.raises(PersonaError) as exc:
            _maybe_downscale(garbage, "image/png")
        assert exc.value.context["reason"] in {"malformed_image", "decode_failed"}


class TestRichOutputServeExtensions:
    """Spec 28 — the uploads serve route (reused per D-28-10) resolves the
    rich-output render-set extensions, not just images. Regression for the
    operator-pass bug F4: text/diagram artifacts 404'd because
    _media_type_for_ext only knew image/office extensions."""

    @pytest.mark.parametrize(
        ("ext", "expected"),
        [
            (".md", "text/markdown"),
            (".mmd", "text/vnd.mermaid"),
            (".dot", "text/vnd.graphviz"),
            (".csv", "text/csv"),
            (".json", "application/json"),
            (".html", "text/html"),
            (".txt", "text/plain"),
            (".py", "text/plain"),
            (".pdf", "application/pdf"),
            (".png", "image/png"),  # image map still works
        ],
    )
    def test_media_type_resolved_for_rich_output_exts(self, ext: str, expected: str) -> None:
        assert image_service._media_type_for_ext(ext) == expected

    def test_fetch_serves_a_markdown_artifact(self, workspace: Path) -> None:
        # Write a markdown artifact where the persister would, then fetch it.
        owner, persona = "user_x", "persona_y"
        uploads = workspace / owner / persona / "uploads"
        uploads.mkdir(parents=True)
        (uploads / "abc.md").write_text("# Hi\n")
        body, media = image_service.fetch(
            file_storage=LocalFileStorage(workspace),
            owner_id=owner,
            persona_id=persona,
            ref="uploads/abc.md",
        )
        assert body == b"# Hi\n"
        assert media == "text/markdown"
