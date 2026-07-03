"""Unit tests for the build-time avatar hook ``_maybe_generate_avatar`` (Spec 29 T3).

These exercise the hook's control flow WITHOUT Postgres: the only DB touch
(``persona_service.set_avatar_url``) is monkeypatched to capture the call, so
the full orchestration — craft → generate_avatar → set avatar_url, plus every
fail-soft branch — runs locally. The true create→avatar_url round-trip against
real Postgres is covered by ``tests/integration/test_api_persona_avatar.py``.

The load-bearing property proven here is **D-29-X-fail-soft**: the hook never
raises into the create path on backend-absent, content-rejection, provider
error, timeout, OR an unexpected exception — it fail-softs to no avatar_url
write and an audit event, so persona-create always succeeds.
"""

# ruff: noqa: ARG002 — test-fake ImageBackend methods conform to the Protocol signature
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from persona.imagegen import (
    ContentRejectedError,
    GeneratedImage,
    GenerationResult,
    ImageGenOptions,
    ImageProviderError,
)
from persona_api.routes import personas as personas_routes
from persona_api.storage import LocalFileStorage

if TYPE_CHECKING:
    from pathlib import Path

    from persona.imagegen.result import ImageMediaType

_TINY_PNG: bytes = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
    "0000000c49444154789c63f8cfc0000003010100c9fe92ef0000000049454e44ae"
    "426082"
)

_OWNER = "u_hook"
_PERSONA = "persona_hook"
_YAML = """\
schema_version: "1.0"
identity:
  name: Astrid
  role: Norwegian tenancy law assistant
  background: |
    Helps tenants understand husleieloven.
  language_default: en
  constraints:
    - Never give binding legal advice.
self_facts:
  - fact: Specialised in Norwegian residential tenancy.
    confidence: 1.0
worldview: []
"""


class _HappyBackend:
    @property
    def provider_name(self) -> str:
        return "fake"

    @property
    def model_name(self) -> str:
        return "fake-1"

    async def generate(
        self, prompt: str, *, options: ImageGenOptions | None = None
    ) -> GenerationResult:
        media: ImageMediaType = "image/png"
        return GenerationResult(
            images=[
                GeneratedImage(
                    image_bytes=_TINY_PNG,
                    workspace_path=None,
                    media_type=media,
                    width=1,
                    height=1,
                    revised_prompt=None,
                )
            ],
            provider=self.provider_name,
            model=self.model_name,
            latency_ms=5.0,
        )

    async def edit(self, *a: object, **k: object) -> GenerationResult:
        raise NotImplementedError


class _RejectingBackend(_HappyBackend):
    async def generate(
        self, prompt: str, *, options: ImageGenOptions | None = None
    ) -> GenerationResult:
        raise ContentRejectedError("nope", context={"reason": "provider_moderation"})


class _ProviderErrorBackend(_HappyBackend):
    async def generate(
        self, prompt: str, *, options: ImageGenOptions | None = None
    ) -> GenerationResult:
        raise ImageProviderError("5xx", context={"reason": "transient"})


class _SlowBackend(_HappyBackend):
    async def generate(
        self, prompt: str, *, options: ImageGenOptions | None = None
    ) -> GenerationResult:
        await asyncio.sleep(5.0)  # cancelled by the wall-clock bound
        return await super().generate(prompt, options=options)


class _CrashingBackend(_HappyBackend):
    async def generate(
        self, prompt: str, *, options: ImageGenOptions | None = None
    ) -> GenerationResult:
        raise RuntimeError("non-domain explosion")  # must NOT escape the hook


def _request(tmp_path: Path, *, backend: object, timeout_s: float = 25.0) -> SimpleNamespace:
    state = SimpleNamespace(
        audit_root=tmp_path / "audit",
        image_backend=backend,
        workspace_root=tmp_path / "ws",
        file_storage=LocalFileStorage(tmp_path / "ws"),
        avatar_gen_timeout_s=timeout_s,
        rls_engine=object(),  # never hit — set_avatar_url is monkeypatched
    )
    return SimpleNamespace(app=SimpleNamespace(state=state))


@pytest.fixture
def captured_set(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    """Capture set_avatar_url calls instead of writing to Postgres."""
    calls: list[dict[str, str]] = []

    def _capture(*, rls_engine: object, persona_id: str, avatar_url: str) -> None:  # noqa: ARG001 — signature must mirror set_avatar_url
        calls.append({"persona_id": persona_id, "avatar_url": avatar_url})

    monkeypatch.setattr(personas_routes.persona_service, "set_avatar_url", _capture)
    return calls


def _run(request: SimpleNamespace) -> None:
    asyncio.run(
        personas_routes._maybe_generate_avatar(
            request,  # type: ignore[arg-type]
            owner_id=_OWNER,
            persona_id=_PERSONA,
            yaml_str=_YAML,
        )
    )


def _audit_outcomes(tmp_path: Path) -> list[str]:
    path = tmp_path / "audit" / f"{_PERSONA}.tools.jsonl"
    if not path.exists():
        return []
    return [json.loads(line)["metadata"]["outcome"] for line in path.read_text().splitlines()]


# ---------------------------------------------------------------------------
# Happy path — avatar_url points at the served uploads route path.
# ---------------------------------------------------------------------------


def test_hook_success_sets_served_avatar_url(
    tmp_path: Path, captured_set: list[dict[str, str]]
) -> None:
    import hashlib

    _run(_request(tmp_path, backend=_HappyBackend()))

    ref = hashlib.blake2b(_TINY_PNG, digest_size=16).hexdigest()
    # avatar_url is the BARE workspace ref (the web authed-image hook builds the
    # served URL); NOT the full /v1/... route path a browser <img> can't auth.
    assert captured_set == [
        {
            "persona_id": _PERSONA,
            "avatar_url": f"uploads/{ref}.png",
        }
    ]
    assert _audit_outcomes(tmp_path) == ["ok"]


# ---------------------------------------------------------------------------
# Fail-soft branches — set_avatar_url NEVER called; the hook never raises.
# ---------------------------------------------------------------------------


def test_hook_backend_absent_fails_soft(tmp_path: Path, captured_set: list[dict[str, str]]) -> None:
    _run(_request(tmp_path, backend=None))
    assert captured_set == []
    outcomes = _audit_outcomes(tmp_path)
    assert outcomes == ["error"]


def test_hook_content_rejection_fails_soft(
    tmp_path: Path, captured_set: list[dict[str, str]]
) -> None:
    _run(_request(tmp_path, backend=_RejectingBackend()))
    assert captured_set == []
    assert _audit_outcomes(tmp_path) == ["content_rejected_provider"]


def test_hook_provider_error_fails_soft(tmp_path: Path, captured_set: list[dict[str, str]]) -> None:
    _run(_request(tmp_path, backend=_ProviderErrorBackend()))
    assert captured_set == []
    assert _audit_outcomes(tmp_path) == ["error"]


def test_hook_timeout_fails_soft(tmp_path: Path, captured_set: list[dict[str, str]]) -> None:
    _run(_request(tmp_path, backend=_SlowBackend(), timeout_s=0.05))
    assert captured_set == []
    outcomes = _audit_outcomes(tmp_path)
    assert outcomes[-1] == "error"


def test_hook_unexpected_exception_never_escapes(
    tmp_path: Path, captured_set: list[dict[str, str]]
) -> None:
    """A non-domain exception from the backend must be swallowed (create must not break)."""
    _run(_request(tmp_path, backend=_CrashingBackend()))  # must not raise
    assert captured_set == []
    assert _audit_outcomes(tmp_path) == ["error"]
