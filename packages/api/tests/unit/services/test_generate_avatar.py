"""Unit tests for :func:`persona_api.imagegen.service.generate_avatar` (Spec 29 T2).

``generate_avatar`` is the build-time, **free** sibling of ``generate``:
no credits engine, no concurrency lock. These tests are therefore pure
unit tests (no Postgres). They verify:

* the happy path persists one square avatar (bytes zeroed, workspace_path
  set) and emits one ``outcome=ok`` audit tagged ``credits_charged=0`` /
  ``system_initiated=true`` (D-29-2 zero-cost system event);
* the **hard-line categorical filter** runs explicitly (D-29-1 defense-in-
  depth backstop — the service path does not otherwise run it): an
  adversarial prompt raises ``ContentRejectedError(reason=hard_line)``,
  emits a hash-only audit, and persists NO bytes (the prompt is never
  written, only its sha256);
* provider moderation (``ContentRejectedError``) and provider failure
  (``ImageGenError`` family) are audited then re-raised for the hook to
  fail-soft, with NO bytes persisted.
"""

# ruff: noqa: ARG002 — test-fake ImageBackend methods conform to the Protocol signature
from __future__ import annotations

import asyncio
import hashlib
from typing import TYPE_CHECKING

import pytest
from persona.imagegen import (
    ContentRejectedError,
    GeneratedImage,
    GenerationResult,
    ImageGenOptions,
    ImageProviderError,
    hash_prompt_for_audit,
)
from persona.tools.audit import MemoryToolAuditLogger
from persona_api.imagegen import service as imagegen_service
from persona_api.storage import LocalFileStorage

if TYPE_CHECKING:
    from pathlib import Path

    from persona.imagegen.result import ImageMediaType

# Minimum-valid 1x1 RGB PNG (mirrors the imagegen service integration test).
_TINY_PNG: bytes = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
    "0000000c49444154789c63f8cfc0000003010100c9fe92ef0000000049454e44ae"
    "426082"
)

_USER = "u_avatar"
_PERSONA = "p_avatar"

# A clean crafted-style prompt (what the crafter actually produces).
_CLEAN_PROMPT = (
    "a professional headshot portrait representing the role of surgeon, "
    "professional attire, neutral studio background, soft even lighting"
)
# An adversarial prompt simulating a tampered/declared visual_style that
# trips the hard-line filter (MINOR ∩ SEX co-occurrence → c1). The crafter
# never produces this, but generate_avatar's filter MUST catch it.
_ADVERSARIAL_PROMPT = "a nude child portrait"


class _HappyBackend:
    """Minimal ImageBackend returning one deterministic PNG."""

    def __init__(self, *, media_type: ImageMediaType = "image/png") -> None:
        self._media_type: ImageMediaType = media_type

    @property
    def provider_name(self) -> str:
        return "fake"

    @property
    def model_name(self) -> str:
        return "fake-model-1"

    async def generate(
        self, prompt: str, *, options: ImageGenOptions | None = None
    ) -> GenerationResult:
        return GenerationResult(
            images=[
                GeneratedImage(
                    image_bytes=_TINY_PNG,
                    workspace_path=None,
                    media_type=self._media_type,
                    width=1,
                    height=1,
                    revised_prompt=None,
                )
            ],
            provider=self.provider_name,
            model=self.model_name,
            latency_ms=9.0,
        )

    async def edit(
        self,
        input_image: GeneratedImage,
        instructions: str,
        *,
        options: ImageGenOptions | None = None,
    ) -> GenerationResult:
        raise NotImplementedError


class _CapturingBackend(_HappyBackend):
    """Records the prompt the backend received (to assert no re-merge)."""

    def __init__(self) -> None:
        super().__init__()
        self.seen: list[str] = []

    async def generate(
        self, prompt: str, *, options: ImageGenOptions | None = None
    ) -> GenerationResult:
        self.seen.append(prompt)
        return await super().generate(prompt, options=options)


class _RejectingBackend(_HappyBackend):
    async def generate(
        self, prompt: str, *, options: ImageGenOptions | None = None
    ) -> GenerationResult:
        raise ContentRejectedError(
            "provider rejected prompt",
            context={"reason": "provider_moderation", "stage": "input"},
        )


class _FailingBackend(_HappyBackend):
    async def generate(
        self, prompt: str, *, options: ImageGenOptions | None = None
    ) -> GenerationResult:
        raise ImageProviderError("transient 5xx", context={"reason": "transient"})


def _run_avatar(
    backend: object,
    tmp_path: Path,
    *,
    prompt: str = _CLEAN_PROMPT,
    audit: MemoryToolAuditLogger | None = None,
) -> GenerationResult:
    return asyncio.run(
        imagegen_service.generate_avatar(
            file_storage=LocalFileStorage(tmp_path / "ws"),
            backend=backend,  # type: ignore[arg-type]
            user_id=_USER,
            persona_id=_PERSONA,
            prompt=prompt,
            audit_logger=audit,
        )
    )


# ---------------------------------------------------------------------------
# Happy path.
# ---------------------------------------------------------------------------


def test_generate_avatar_persists_one_square_image(tmp_path: Path) -> None:
    audit = MemoryToolAuditLogger()
    result = _run_avatar(_HappyBackend(), tmp_path, audit=audit)

    assert len(result.images) == 1
    img = result.images[0]
    assert img.image_bytes == b"", "image_bytes must be zeroed after persistence"
    ref = hashlib.blake2b(_TINY_PNG, digest_size=16).hexdigest()
    assert img.workspace_path == f"uploads/{ref}.png"
    on_disk = tmp_path / "ws" / _USER / _PERSONA / "uploads" / f"{ref}.png"
    assert on_disk.is_file()
    assert on_disk.read_bytes() == _TINY_PNG


def test_generate_avatar_emits_zero_cost_system_audit(tmp_path: Path) -> None:
    audit = MemoryToolAuditLogger()
    _run_avatar(_HappyBackend(), tmp_path, audit=audit)

    assert len(audit.events) == 1
    ev = audit.events[0]
    assert ev.tool_name == "generate_avatar"
    assert ev.persona_id == _PERSONA
    assert ev.is_error is False
    assert ev.metadata["outcome"] == "ok"
    # D-29-2: visible as a free, system-initiated event.
    assert ev.metadata["credits_charged"] == "0"
    assert ev.metadata["system_initiated"] == "true"


def test_generate_avatar_does_not_re_merge_visual_style(tmp_path: Path) -> None:
    """The prompt is already crafted; generate_avatar passes it to the backend verbatim."""
    backend = _CapturingBackend()
    _run_avatar(backend, tmp_path, prompt=_CLEAN_PROMPT)
    assert backend.seen == [_CLEAN_PROMPT]


def test_generate_avatar_takes_no_credits_engine() -> None:
    """Structural D-29-2: the signature has no rls_engine / cost params (free + cap-free)."""
    import inspect

    params = set(inspect.signature(imagegen_service.generate_avatar).parameters)
    assert "rls_engine" not in params
    assert "cost_per_image_credits" not in params


# ---------------------------------------------------------------------------
# Hard-line filter backstop (D-29-1 defense-in-depth).
# ---------------------------------------------------------------------------


def test_generate_avatar_hard_line_rejects_and_does_not_call_provider(tmp_path: Path) -> None:
    audit = MemoryToolAuditLogger()
    backend = _CapturingBackend()

    with pytest.raises(ContentRejectedError) as excinfo:
        _run_avatar(backend, tmp_path, prompt=_ADVERSARIAL_PROMPT, audit=audit)

    # Provider was never consulted.
    assert backend.seen == []
    assert excinfo.value.context.get("reason") == "hard_line"
    assert excinfo.value.context.get("category") == "c1"

    # Audit: hash-only, prompt never persisted.
    assert len(audit.events) == 1
    ev = audit.events[0]
    assert ev.metadata["outcome"] == "content_rejected_hard_line"
    assert ev.metadata["prompt_sha256"] == hash_prompt_for_audit(_ADVERSARIAL_PROMPT)
    assert _ADVERSARIAL_PROMPT not in ev.model_dump_json()

    # No bytes on disk.
    uploads = tmp_path / "ws" / _USER / _PERSONA / "uploads"
    assert not uploads.exists() or list(uploads.iterdir()) == []


# ---------------------------------------------------------------------------
# Provider-side failures — audited then re-raised; no bytes persisted.
# ---------------------------------------------------------------------------


def test_generate_avatar_provider_rejection_audits_and_raises(tmp_path: Path) -> None:
    audit = MemoryToolAuditLogger()
    with pytest.raises(ContentRejectedError):
        _run_avatar(_RejectingBackend(), tmp_path, audit=audit)

    assert audit.events[0].metadata["outcome"] == "content_rejected_provider"
    uploads = tmp_path / "ws" / _USER / _PERSONA / "uploads"
    assert not uploads.exists() or list(uploads.iterdir()) == []


def test_generate_avatar_provider_error_audits_and_raises(tmp_path: Path) -> None:
    audit = MemoryToolAuditLogger()
    with pytest.raises(ImageProviderError):
        _run_avatar(_FailingBackend(), tmp_path, audit=audit)

    ev = audit.events[0]
    assert ev.metadata["outcome"] == "error"
    assert ev.metadata["error_type"] == "ImageProviderError"
    uploads = tmp_path / "ws" / _USER / _PERSONA / "uploads"
    assert not uploads.exists() or list(uploads.iterdir()) == []


def test_generate_avatar_no_audit_logger_is_safe(tmp_path: Path) -> None:
    """Audit logger is optional — a None sink must not crash the happy path."""
    result = _run_avatar(_HappyBackend(), tmp_path, audit=None)
    assert result.images[0].workspace_path is not None
