"""Integration tests for :func:`persona_api.imagegen.service.generate` (spec 15 T15).

Three scenarios per ``tasks.md`` §T15 acceptance bullets:

1. **Happy path** — bytes land on disk at the expected D-13-4 layout
   (``uploads/<blake2b><ext>``); the credits ledger shows exactly one
   ``-count*cost_per_image_credits`` entry; the returned
   :class:`GenerationResult` carries images with ``workspace_path``
   populated and ``image_bytes`` zeroed.
2. **Backend failure → refund applied** — the mocked backend raises
   :class:`ContentRejectedError`; the credits ledger shows a matching
   deduct + refund pair (net zero); NO bytes land on disk; the original
   exception propagates to the caller (so the route layer can map it to
   HTTP 422).
3. **Concurrency-capped → no credits touched** — a held advisory lock on
   the same ``user_id`` causes the second call to raise
   :class:`ConcurrencyCappedError`; the credits ledger is unchanged
   (the cap fires BEFORE the deduct so the rollback leaves the balance
   intact); NO bytes land on disk; NO refund entry appears.

These scenarios exercise the structural correctness of the three cost-
discipline locks per decisions.md gate paragraph #3:

* pre-deduct (D-15-X-pre-deduct-credits): scenario 2 proves the deduct
  lands BEFORE the backend call (the refund in the ledger is evidence).
* refund-on-failure (D-15-X-credit-flow-semantics pattern (a)): scenario
  2 proves the refund issues via :func:`credits_service.refund`.
* concurrency cap (D-15-X-concurrency-cap): scenario 3 proves the cap
  blocks parallel-fire before any credit movement.

Tests use the ``migrated_engine`` fixture (real Postgres 16 + Alembic
migrations) so the advisory lock primitive is exercised against the real
Postgres feature it relies on; mocking the lock would invalidate the
property under test.
"""

# ruff: noqa: ANN401, ARG001, ARG002, E501
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
)
from persona_api.db.models import credit_transactions as credit_tx_t
from persona_api.db.models import credits as credits_t
from persona_api.errors import ConcurrencyCappedError
from persona_api.imagegen import service as imagegen_service
from persona_api.imagegen.concurrency import acquire_user_concurrency
from persona_api.storage import LocalFileStorage
from sqlalchemy import select, text

if TYPE_CHECKING:
    from pathlib import Path

    from persona.imagegen.result import ImageMediaType
    from sqlalchemy import Engine

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Test PNG bytes — minimum-valid 1x1 RGB PNG (mirrors test_workspace_cascade
# and test_uploads). The two variants differ by a single byte so the
# blake2b content-hash differs and the idempotent content-addressed write
# does NOT collapse two distinct images into one file.
# ---------------------------------------------------------------------------


_TINY_PNG_A: bytes = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
    "0000000c49444154789c63f8cfc0000003010100c9fe92ef0000000049454e44ae"
    "426082"
)
_TINY_PNG_B: bytes = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
    "0000000c49444154789c63f8cfc0000003010100c9fe92f00000000049454e44ae"
    "426082"
)


# ---------------------------------------------------------------------------
# Test fakes — controlled :class:`ImageBackend` implementations so the
# service-level test does not depend on the openai / fal SDKs.
# ---------------------------------------------------------------------------


class _HappyBackend:
    """Mock backend that returns deterministic bytes from a fixed list.

    Implements the :class:`persona.imagegen.protocol.ImageBackend`
    Protocol structurally (duck-typed; ``runtime_checkable`` on the
    Protocol verifies this works at runtime).
    """

    def __init__(
        self,
        *,
        image_bytes_list: list[bytes],
        media_type: ImageMediaType = "image/png",
    ) -> None:
        self._image_bytes_list = image_bytes_list
        self._media_type: ImageMediaType = media_type

    @property
    def provider_name(self) -> str:
        return "fake"

    @property
    def model_name(self) -> str:
        return "fake-model-1"

    async def generate(
        self,
        prompt: str,
        *,
        options: ImageGenOptions | None = None,
    ) -> GenerationResult:
        # Honour ``options.count`` by slicing the pre-seeded byte list;
        # the count cap (D-15-3, le=2) is enforced upstream by the
        # ImageGenOptions Pydantic field.
        n = options.count if options is not None else 1
        images = [
            GeneratedImage(
                image_bytes=b,
                workspace_path=None,
                media_type=self._media_type,
                width=1,
                height=1,
                revised_prompt=None,
            )
            for b in self._image_bytes_list[:n]
        ]
        return GenerationResult(
            images=images,
            provider=self.provider_name,
            model=self.model_name,
            latency_ms=12.5,
        )

    async def edit(
        self,
        input_image: GeneratedImage,
        instructions: str,
        *,
        options: ImageGenOptions | None = None,
    ) -> GenerationResult:
        raise NotImplementedError("edit not supported in v1")


class _RejectingBackend:
    """Mock backend that raises :class:`ContentRejectedError` (provider moderation)."""

    def __init__(self) -> None:
        pass

    @property
    def provider_name(self) -> str:
        return "fake"

    @property
    def model_name(self) -> str:
        return "fake-model-rejecting"

    async def generate(
        self,
        prompt: str,
        *,
        options: ImageGenOptions | None = None,
    ) -> GenerationResult:
        raise ContentRejectedError(
            "provider rejected prompt",
            context={
                "reason": "provider_moderation",
                "stage": "input",
                "provider": self.provider_name,
                "model": self.model_name,
            },
        )

    async def edit(
        self,
        input_image: GeneratedImage,
        instructions: str,
        *,
        options: ImageGenOptions | None = None,
    ) -> GenerationResult:
        raise NotImplementedError("edit not supported in v1")


class _FailingBackend:
    """Mock backend that raises :class:`ImageProviderError` (transient)."""

    @property
    def provider_name(self) -> str:
        return "fake"

    @property
    def model_name(self) -> str:
        return "fake-model-failing"

    async def generate(
        self,
        prompt: str,
        *,
        options: ImageGenOptions | None = None,
    ) -> GenerationResult:
        raise ImageProviderError(
            "transient provider 5xx",
            context={
                "reason": "transient",
                "provider": self.provider_name,
                "model": self.model_name,
            },
        )

    async def edit(
        self,
        input_image: GeneratedImage,
        instructions: str,
        *,
        options: ImageGenOptions | None = None,
    ) -> GenerationResult:
        raise NotImplementedError("edit not supported in v1")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_USER = "u_imagegen_svc"
_PERSONA = "p_imagegen_svc"


@pytest.fixture
def seeded_engine(migrated_engine: Engine) -> Engine:
    """Insert the FK target user row + a stub persona row so credits + workspace writes don't trip FKs.

    The credits table has a CASCADE FK to ``users.id``; we insert the
    user. The persona row isn't required for the service.generate flow
    (the workspace path uses the persona id segment but doesn't FK-check
    against ``personas``), but a future test refactor might want it — we
    only seed the user here.
    """
    with migrated_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
            {"i": _USER, "e": f"{_USER}@x.test"},
        )
    return migrated_engine


def _balance(engine: Engine, user_id: str) -> int:
    with engine.begin() as conn:
        row = conn.execute(
            select(credits_t.c.balance).where(credits_t.c.user_id == user_id)
        ).first()
    assert row is not None, "credits row missing for user"
    return int(row[0])


def _tx_deltas(engine: Engine, user_id: str) -> list[int]:
    with engine.begin() as conn:
        rows = conn.execute(
            select(credit_tx_t.c.delta, credit_tx_t.c.reason)
            .where(credit_tx_t.c.user_id == user_id)
            .order_by(credit_tx_t.c.created_at.asc(), credit_tx_t.c.id.asc())
        ).all()
    return [int(r[0]) for r in rows]


def _tx_reasons(engine: Engine, user_id: str) -> list[str]:
    with engine.begin() as conn:
        rows = conn.execute(
            select(credit_tx_t.c.reason)
            .where(credit_tx_t.c.user_id == user_id)
            .order_by(credit_tx_t.c.created_at.asc(), credit_tx_t.c.id.asc())
        ).all()
    return [str(r[0]) for r in rows]


# ---------------------------------------------------------------------------
# Scenario 1: happy path — bytes on disk, credits deducted, no refund.
# ---------------------------------------------------------------------------


def test_generate_happy_path_persists_bytes_and_deducts_credits(
    seeded_engine: Engine,
    tmp_path: Path,
) -> None:
    """A successful generation lands bytes at the D-13-4 workspace path AND deducts credits exactly once."""
    backend = _HappyBackend(image_bytes_list=[_TINY_PNG_A])
    options = ImageGenOptions(size="1024x1024", count=1, quality="standard")

    start_balance = _balance(seeded_engine, _USER) if _has_row(seeded_engine, _USER) else 100_000

    result = asyncio.run(
        imagegen_service.generate(
            rls_engine=seeded_engine,
            file_storage=LocalFileStorage(tmp_path / "workspace"),
            backend=backend,
            user_id=_USER,
            persona_id=_PERSONA,
            persona_visual_style=None,
            prompt="a red bicycle",
            options=options,
            cost_per_image_credits=100,
        )
    )

    # Result envelope: image_bytes zeroed, workspace_path populated.
    assert isinstance(result, GenerationResult)
    assert len(result.images) == 1
    img = result.images[0]
    assert img.image_bytes == b"", "service must zero image_bytes after persistence"
    assert img.workspace_path is not None
    expected_ref = hashlib.blake2b(_TINY_PNG_A, digest_size=16).hexdigest()
    assert img.workspace_path == f"uploads/{expected_ref}.png"
    assert img.media_type == "image/png"
    assert img.width == 1
    assert img.height == 1

    # Bytes on disk at the D-13-4 layout.
    expected_path = tmp_path / "workspace" / _USER / _PERSONA / "uploads" / f"{expected_ref}.png"
    assert expected_path.is_file()
    assert expected_path.read_bytes() == _TINY_PNG_A

    # Credits: exactly one deduct, no refund.
    deltas = _tx_deltas(seeded_engine, _USER)
    assert deltas == [-100], f"expected one -100 deduct, got {deltas}"
    reasons = _tx_reasons(seeded_engine, _USER)
    assert reasons == ["image_gen_pre"]
    assert _balance(seeded_engine, _USER) == start_balance - 100


def test_generate_happy_path_with_count_2_persists_two_files(
    seeded_engine: Engine,
    tmp_path: Path,
) -> None:
    """``count=2`` (D-15-3 ceiling) lands two distinct files and deducts ``2 * cost``."""
    backend = _HappyBackend(image_bytes_list=[_TINY_PNG_A, _TINY_PNG_B])
    options = ImageGenOptions(size="1024x1024", count=2, quality="standard")

    result = asyncio.run(
        imagegen_service.generate(
            rls_engine=seeded_engine,
            file_storage=LocalFileStorage(tmp_path / "workspace"),
            backend=backend,
            user_id=_USER,
            persona_id=_PERSONA,
            persona_visual_style=None,
            prompt="two illustrations",
            options=options,
            cost_per_image_credits=100,
        )
    )

    assert len(result.images) == 2
    paths = [img.workspace_path for img in result.images]
    assert paths[0] != paths[1], "distinct bytes must hash to distinct workspace paths"
    for img in result.images:
        assert img.image_bytes == b""
        assert img.workspace_path is not None
        on_disk = tmp_path / "workspace" / _USER / _PERSONA / img.workspace_path
        assert on_disk.is_file()

    deltas = _tx_deltas(seeded_engine, _USER)
    assert deltas == [-200], f"expected one -200 deduct (count=2 * 100), got {deltas}"


def test_generate_happy_path_merges_visual_style_into_prompt(
    seeded_engine: Engine,
    tmp_path: Path,
) -> None:
    """The persona's ``visual_style`` is merged at the service layer so the backend sees the merged prompt (D-15-4)."""
    captured: list[str] = []

    class _CapturingBackend(_HappyBackend):
        async def generate(
            self,
            prompt: str,
            *,
            options: ImageGenOptions | None = None,
        ) -> GenerationResult:
            captured.append(prompt)
            return await super().generate(prompt, options=options)

    backend = _CapturingBackend(image_bytes_list=[_TINY_PNG_A])
    asyncio.run(
        imagegen_service.generate(
            rls_engine=seeded_engine,
            file_storage=LocalFileStorage(tmp_path / "workspace"),
            backend=backend,
            user_id=_USER,
            persona_id=_PERSONA,
            persona_visual_style="watercolour",
            prompt="a cat",
            options=ImageGenOptions(),
            cost_per_image_credits=100,
        )
    )
    assert captured == ["a cat, in the style of watercolour"], (
        "visual_style suffix-conditioning must run BEFORE the backend call"
    )


# ---------------------------------------------------------------------------
# Scenario 2: backend failure → refund applied; no bytes on disk; original
# exception propagates.
# ---------------------------------------------------------------------------


def test_generate_backend_content_rejection_refunds_credits(
    seeded_engine: Engine,
    tmp_path: Path,
) -> None:
    """Provider moderation rejection: deduct lands, refund issues, exception propagates, NO bytes on disk."""
    backend = _RejectingBackend()
    options = ImageGenOptions(size="1024x1024", count=1, quality="standard")
    start_balance = _balance(seeded_engine, _USER) if _has_row(seeded_engine, _USER) else 100_000

    with pytest.raises(ContentRejectedError) as excinfo:
        asyncio.run(
            imagegen_service.generate(
                rls_engine=seeded_engine,
                file_storage=LocalFileStorage(tmp_path / "workspace"),
                backend=backend,
                user_id=_USER,
                persona_id=_PERSONA,
                persona_visual_style=None,
                prompt="anything",
                options=options,
                cost_per_image_credits=100,
            )
        )

    # The provider context survives the funnel (route layer needs it).
    assert excinfo.value.context.get("reason") == "provider_moderation"
    assert excinfo.value.context.get("stage") == "input"

    # Credits: deduct + refund pair (audit trail captures the round trip).
    deltas = _tx_deltas(seeded_engine, _USER)
    assert deltas == [-100, 100], f"expected deduct then refund, got {deltas}"
    reasons = _tx_reasons(seeded_engine, _USER)
    assert reasons[0] == "image_gen_pre"
    assert reasons[1] == "image_gen_refund:backend_failure"
    # Net zero — the user's balance is unchanged.
    assert _balance(seeded_engine, _USER) == start_balance

    # No bytes on disk — the failure branch persists nothing.
    workspace = tmp_path / "workspace" / _USER / _PERSONA / "uploads"
    if workspace.exists():
        assert list(workspace.iterdir()) == [], (
            "no files must land when the backend rejected the prompt"
        )


def test_generate_backend_transient_error_refunds_credits(
    seeded_engine: Engine,
    tmp_path: Path,
) -> None:
    """:class:`ImageProviderError` (non-moderation) also triggers the refund-on-failure path."""
    backend = _FailingBackend()
    start_balance = _balance(seeded_engine, _USER) if _has_row(seeded_engine, _USER) else 100_000

    with pytest.raises(ImageProviderError):
        asyncio.run(
            imagegen_service.generate(
                rls_engine=seeded_engine,
                file_storage=LocalFileStorage(tmp_path / "workspace"),
                backend=backend,
                user_id=_USER,
                persona_id=_PERSONA,
                persona_visual_style=None,
                prompt="anything",
                options=ImageGenOptions(),
                cost_per_image_credits=100,
            )
        )

    deltas = _tx_deltas(seeded_engine, _USER)
    assert deltas == [-100, 100]
    assert _balance(seeded_engine, _USER) == start_balance


# ---------------------------------------------------------------------------
# Scenario 3: concurrency-capped → no credits touched.
# ---------------------------------------------------------------------------


def test_generate_concurrency_capped_does_not_touch_credits(
    seeded_engine: Engine,
    tmp_path: Path,
) -> None:
    """Holding the advisory lock on a separate connection causes generate() to raise BEFORE deducting.

    This is the binary structural proof of D-15-X-concurrency-cap +
    D-15-X-pre-deduct-credits combined: the cap fires INSIDE the same
    transaction that owns the deduct, so when the cap raises
    ``ConcurrencyCappedError`` the surrounding ``with rls_engine.begin()``
    rolls back and the deduct never happens. No refund needed either —
    the ledger is unchanged.
    """
    backend = _HappyBackend(image_bytes_list=[_TINY_PNG_A])
    start_balance = _balance(seeded_engine, _USER) if _has_row(seeded_engine, _USER) else 100_000

    # Hold the advisory lock on a separate connection so the service's
    # cap acquisition observes ``acquired=False``. The held lock simulates
    # a concurrent in-flight generation for the same user.
    with seeded_engine.connect() as holder_conn:
        holder_trans = holder_conn.begin()
        try:
            row = holder_conn.execute(
                text(
                    "SELECT pg_try_advisory_xact_lock("
                    "('x' || md5(:uid))::bit(64)::bigint) AS acquired"
                ),
                {"uid": _USER},
            ).first()
            assert row is not None
            assert bool(row.acquired) is True

            # The second call (the service under test) must hit ``acquired=False``.
            with pytest.raises(ConcurrencyCappedError) as excinfo:
                asyncio.run(
                    imagegen_service.generate(
                        rls_engine=seeded_engine,
                        file_storage=LocalFileStorage(tmp_path / "workspace"),
                        backend=backend,
                        user_id=_USER,
                        persona_id=_PERSONA,
                        persona_visual_style=None,
                        prompt="anything",
                        options=ImageGenOptions(),
                        cost_per_image_credits=100,
                    )
                )

            assert excinfo.value.context.get("user_id") == _USER
            assert excinfo.value.context.get("retry_after_s") == "5"
        finally:
            holder_trans.rollback()

    # Credits ledger: completely unchanged.
    deltas = _tx_deltas(seeded_engine, _USER)
    assert deltas == [], f"concurrency-capped path must NOT touch the credits ledger; got {deltas}"
    if _has_row(seeded_engine, _USER):
        assert _balance(seeded_engine, _USER) == start_balance

    # No bytes on disk.
    workspace = tmp_path / "workspace" / _USER / _PERSONA / "uploads"
    if workspace.exists():
        assert list(workspace.iterdir()) == []


def test_generate_after_concurrency_cap_releases_succeeds(
    seeded_engine: Engine,
    tmp_path: Path,
) -> None:
    """Once the held lock is released the service.generate path succeeds normally — the cap is in-flight, not permanent."""
    backend = _HappyBackend(image_bytes_list=[_TINY_PNG_A])

    # First call: cap blocks.
    with seeded_engine.connect() as holder_conn:
        holder_trans = holder_conn.begin()
        try:
            row = holder_conn.execute(
                text(
                    "SELECT pg_try_advisory_xact_lock("
                    "('x' || md5(:uid))::bit(64)::bigint) AS acquired"
                ),
                {"uid": _USER},
            ).first()
            assert row is not None
            assert bool(row.acquired) is True

            with pytest.raises(ConcurrencyCappedError):
                asyncio.run(
                    imagegen_service.generate(
                        rls_engine=seeded_engine,
                        file_storage=LocalFileStorage(tmp_path / "workspace"),
                        backend=backend,
                        user_id=_USER,
                        persona_id=_PERSONA,
                        persona_visual_style=None,
                        prompt="x",
                        options=ImageGenOptions(),
                        cost_per_image_credits=100,
                    )
                )
        finally:
            holder_trans.rollback()

    # Second call (after the holder released): proceeds normally.
    result = asyncio.run(
        imagegen_service.generate(
            rls_engine=seeded_engine,
            file_storage=LocalFileStorage(tmp_path / "workspace"),
            backend=backend,
            user_id=_USER,
            persona_id=_PERSONA,
            persona_visual_style=None,
            prompt="x",
            options=ImageGenOptions(),
            cost_per_image_credits=100,
        )
    )
    assert len(result.images) == 1
    assert result.images[0].workspace_path is not None
    deltas = _tx_deltas(seeded_engine, _USER)
    assert deltas == [-100], "second call (lock free) deducts normally"


# ---------------------------------------------------------------------------
# Helper — checks if a credits row exists for the user (to safely call _balance).
# ---------------------------------------------------------------------------


def _has_row(engine: Engine, user_id: str) -> bool:
    with engine.begin() as conn:
        row = conn.execute(
            select(credits_t.c.user_id).where(credits_t.c.user_id == user_id)
        ).first()
    return row is not None


# ---------------------------------------------------------------------------
# Sanity check: the acquire_user_concurrency helper is re-exported through
# the package init so callers (route layer in T16) can import it via
# ``persona_api.imagegen`` rather than reaching for the submodule.
# ---------------------------------------------------------------------------


def test_acquire_user_concurrency_re_exported_from_package_init() -> None:
    """The package init surfaces the public helpers used by callers."""
    from persona_api.imagegen import acquire_user_concurrency as _from_init

    assert _from_init is acquire_user_concurrency
