"""Parallel-fire pre-deduct regression test (spec 15 T17).

The binary structural proof of D-15-X-pre-deduct-credits +
D-15-X-concurrency-cap *together*: fires 10 concurrent
``POST /v1/personas/:id/imagegen`` requests from one user against a
mocked backend that holds the request open for ~500ms then returns a
single image, and asserts that exactly ONE deduct lands in the credits
ledger, exactly NINE 429s are returned, exactly ONE 201 is returned,
exactly ONE ``imagegen.create`` audit row is recorded, and exactly ONE
image file ends up on disk for this user.

Why this is the binary proof of the two decisions combined:

* **Without pre-deduct** (D-15-X-pre-deduct-credits removed → deduct
  AFTER the backend returns), 10 concurrent in-flight calls could fire
  10 backend calls before any deduction lands → ``final_balance ==
  start_balance - 10 × per_image_cost`` (or, with refund-on-failure,
  zero — but no protection against the denial-of-wallet amplifier).
* **Without the concurrency cap** (D-15-X-concurrency-cap removed),
  all 10 deductions would land in parallel (each in its own
  transaction) and 10 backend calls would fire → again ``-10 × cost``.
* **Without both** (the world before spec 15 imagined this cost
  discipline existed), nothing structurally prevents the amplifier.

With BOTH locks combined the structural property is binary:

* The per-user advisory lock allows exactly ONE request to hold the
  slot at a time. The other nine see ``acquired=False`` and raise
  :class:`persona_api.errors.ConcurrencyCappedError` → 429 BEFORE the
  pre-deduct fires (the cap raise happens inside the same
  ``rls_engine.begin()`` block that owns the deduct, BEFORE the
  ``credits_service.deduct`` call).
* The one request that holds the slot pre-deducts EXACTLY ONCE, calls
  the backend (which holds for ~500ms — long enough that the other
  nine threads have all observed the lock as held), receives bytes,
  persists them to the workspace, audits, and returns 201.

The test fires requests from 10 OS threads via a
:class:`~concurrent.futures.ThreadPoolExecutor`, synchronised by a
:class:`threading.Barrier` so all 10 enter the route layer
near-simultaneously. The test uses the real Docker Postgres
(``migrated_engine`` fixture) so the advisory-lock primitive is
exercised against the real Postgres feature it relies on; mocking the
lock would invalidate the very property under test.

References:
    docs/specs/phase2/spec_15/decisions.md gate paragraph #3 (cost
    discipline = pre-deduct + cap + refund);
    D-15-X-pre-deduct-credits; D-15-X-concurrency-cap;
    docs/specs/phase2/spec_15/tasks.md §T17.
"""

# ruff: noqa: ANN401, ARG001, ARG002, E501
from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from persona.imagegen import GeneratedImage, GenerationResult, ImageGenOptions
from persona_api.app import create_app
from persona_api.auth import AuthenticatedUser
from persona_api.config import APIConfig
from persona_api.db.models import audit_log as audit_log_t
from persona_api.db.models import credit_transactions as credit_tx_t
from persona_api.db.models import credits as credits_t
from persona_api.middleware.rls_context import make_rls_engine
from sqlalchemy import select, text

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from persona.imagegen.result import ImageMediaType
    from sqlalchemy import Engine
    from tests.conftest import HashEmbedder384

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Test constants and fixtures
# ---------------------------------------------------------------------------


_VALID_YAML = """\
schema_version: "1.0"
identity:
  name: Astrid
  role: Norwegian tenancy law assistant
  background: |
    Helps tenants understand husleieloven.
  language_default: en
  constraints: []
"""


# Minimum-valid 1x1 RGB PNG (mirrors test_imagegen_service + test_api_imagegen).
_TINY_PNG: bytes = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
    "0000000c49444154789c63f8cfc0000003010100c9fe92ef0000000049454e44ae"
    "426082"
)


#: How long the mock backend holds the request open. Long enough for the
#: other nine concurrent requests to all observe the advisory lock as
#: held by the first request (which must commit BEFORE the holder
#: releases) — and short enough to keep the test fast. 500ms matches
#: ``tasks.md`` §T17's scope ("holds ~500ms then returns success").
_BACKEND_HOLD_SECONDS: float = 0.5


#: Spec M3 (T3a): the per-image CEILING pre-deducted before the provider call
#: (``config.image_ceiling_credits`` default) and the real charge the true-up
#: settles to — the slow backend reports no usage, so the price is unpriced and
#: falls to ``config.image_credit_floor`` (default 1).
_IMAGE_CEILING: int = 50
_REAL_CHARGE_FLOOR: int = 1


#: How many concurrent requests to fire. ``tasks.md`` §T17 specifies 10.
_PARALLEL_REQUESTS: int = 10


class _SlowBackend:
    """Mock backend that holds for ``_BACKEND_HOLD_SECONDS`` then returns one PNG.

    Implements the :class:`persona.imagegen.protocol.ImageBackend`
    Protocol structurally (duck-typed; ``runtime_checkable`` on the
    Protocol verifies this works at runtime). The hold uses
    :func:`asyncio.sleep` so the event loop yields during the wait —
    mirrors a real provider call (we ``await`` an HTTP roundtrip, not a
    CPU-bound block) — yet the Postgres advisory lock that the service
    layer acquired *before* this await remains held, exactly as it
    would for a real provider call.
    """

    def __init__(
        self,
        *,
        hold_seconds: float = _BACKEND_HOLD_SECONDS,
        media_type: ImageMediaType = "image/png",
    ) -> None:
        self._hold_seconds = hold_seconds
        self._media_type: ImageMediaType = media_type
        #: Tracks how many times :meth:`generate` was actually entered.
        #: With the cap + pre-deduct combination, exactly ONE call must
        #: reach the backend even when 10 requests fired in parallel.
        self.call_count: int = 0
        self._lock = threading.Lock()

    @property
    def provider_name(self) -> str:
        return "fake"

    @property
    def model_name(self) -> str:
        return "fake-model-slow"

    async def generate(
        self,
        prompt: str,
        *,
        options: ImageGenOptions | None = None,
    ) -> GenerationResult:
        with self._lock:
            self.call_count += 1
        # Hold the request open. ``asyncio.sleep`` yields the event
        # loop; the surrounding service-layer transaction (which owns
        # the advisory lock) remains open on the underlying psycopg
        # connection — exactly the property under test.
        await asyncio.sleep(self._hold_seconds)
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
            latency_ms=self._hold_seconds * 1000.0,
        )

    async def edit(
        self,
        input_image: GeneratedImage,
        instructions: str,
        *,
        options: ImageGenOptions | None = None,
    ) -> GenerationResult:
        raise NotImplementedError("edit not supported in v1")


_USER_A = "u_imagegen_parallel_fire"


@pytest.fixture
def parallel_client(
    migrated_engine: Engine,
    embedder: HashEmbedder384,
    tmp_path: Path,
) -> Iterator[tuple[TestClient, str, Path, Engine, _SlowBackend]]:
    """Real FastAPI client + one seeded user + workspace + admin engine + the slow backend.

    Mirrors :func:`tests.integration.test_api_imagegen.client` but
    yields a tuple shaped for the parallel-fire scenario (one user; no
    second-user; the slow backend handle so the test can read
    ``call_count`` post-hoc).
    """
    import os

    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")

    workspace_root = tmp_path / "workspace"
    cfg = APIConfig(
        app_database_url=app_url,
        audit_root=str(tmp_path / "audit"),
        workspace_root=workspace_root,
    )
    app = create_app(cfg)

    async def _fake_verify(token: str) -> AuthenticatedUser:
        return AuthenticatedUser(id=token, email=None)

    slow_backend = _SlowBackend()

    with TestClient(app) as c:
        app.state.verify_token = _fake_verify
        app.state.embedder = embedder
        # Drop the lifespan-installed TierRegistry so the persona-detail
        # capabilities surface doesn't lazily instantiate a real chat backend
        # (AuthenticationError("missing API key") on CI without ANTHROPIC_API_KEY).
        if hasattr(app.state, "tier_registry"):
            app.state.tier_registry = None
        app.state.image_backend = slow_backend

        # Seed the user as superuser (FK target for personas.owner_id +
        # credits.user_id).
        su = make_rls_engine(os.environ["DATABASE_URL"])
        with su.begin() as conn:
            conn.execute(
                text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
                {"i": _USER_A, "e": f"{_USER_A}@x.test"},
            )
        yield c, _USER_A, workspace_root, su, slow_backend
        # Cleanup
        with su.begin() as conn:
            conn.execute(
                text("DELETE FROM users WHERE id = :i"),
                {"i": _USER_A},
            )
        su.dispose()


def _auth(user_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {user_id}"}


def _create_persona(c: TestClient, user_id: str) -> str:
    resp = c.post("/v1/personas", json={"yaml": _VALID_YAML}, headers=_auth(user_id))
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


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
            select(credit_tx_t.c.delta)
            .where(credit_tx_t.c.user_id == user_id)
            .order_by(credit_tx_t.c.created_at.asc(), credit_tx_t.c.id.asc())
        ).all()
    return [int(r[0]) for r in rows]


def _imagegen_audit_count(engine: Engine, user_id: str) -> int:
    with engine.begin() as conn:
        rows = conn.execute(
            select(audit_log_t.c.id).where(
                (audit_log_t.c.user_id == user_id) & (audit_log_t.c.action == "imagegen.create")
            )
        ).all()
    return len(rows)


def _count_workspace_image_files(workspace_root: Path, user_id: str, persona_id: str) -> int:
    """Count IMAGE files under ``{workspace_root}/{user_id}/{persona_id}/uploads/``.

    Skips F5 ``.f5.json`` + Spec 14 ``.meta.json`` sidecar files via
    :func:`persona_api.services.artifact_metadata.is_any_sidecar` — the
    blake2b-content-addressed image bytes are the primary deliverable; sidecars
    are enrichment co-located with the bytes (F5 L6c — D-F5-X-artifact-metadata-convention).
    """
    from persona_api.services.artifact_metadata import is_any_sidecar

    uploads_dir = workspace_root / user_id / persona_id / "uploads"
    if not uploads_dir.exists():
        return 0
    return sum(1 for f in uploads_dir.iterdir() if f.is_file() and not is_any_sidecar(f))


# ---------------------------------------------------------------------------
# T17 — parallel-fire pre-deduct + concurrency-cap regression test.
# ---------------------------------------------------------------------------


def test_parallel_fire_only_one_request_succeeds_and_deducts(
    parallel_client: tuple[TestClient, str, Path, Engine, _SlowBackend],
) -> None:
    """10 concurrent POST /imagegen → exactly 1 × 201, 9 × 429, 1 × deduct, 1 × audit, 1 × image file.

    Binary structural proof of D-15-X-pre-deduct-credits +
    D-15-X-concurrency-cap combined: removing either lock would let
    ``call_count`` exceed 1 (without the cap) OR let multiple deducts
    land (without pre-deduct) OR allow denial-of-wallet amplification
    (without both). The test holds the binary invariant tight.
    """
    c, uid_a, workspace_root, su, slow_backend = parallel_client
    # Spec 29: persona-create auto-generates an avatar via the wired image
    # backend — which would add an extra slow_backend call (inflating the
    # cap-holder count) + an extra workspace file. Disable avatar-gen during
    # create (fail-soft, no backend call, no bytes) so the assertions below
    # measure ONLY the parallel imagegen fire.
    c.app.state.image_backend = None  # type: ignore[attr-defined]
    pid = _create_persona(c, uid_a)
    c.app.state.image_backend = slow_backend  # type: ignore[attr-defined]

    # Ensure the credits row exists at the default balance BEFORE the
    # parallel fire so the starting balance is deterministic. The
    # ``require_credits`` route gate calls ``ensure_balance`` which
    # inserts the default 100_000 row; we materialise it here so the
    # starting balance does not race with the 10 concurrent calls.
    with su.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO credits (user_id, balance) VALUES (:i, 100000)"
                " ON CONFLICT (user_id) DO UPDATE SET balance = 100000"
            ),
            {"i": uid_a},
        )
    start_balance = _balance(su, uid_a)
    assert start_balance == 100_000

    # Barrier ensures all 10 worker threads enter the POST near-
    # simultaneously so the 10 requests race through the advisory-lock
    # acquisition window together. Without the barrier the first thread
    # might complete its full 500ms generation before the second thread
    # even fires its request — the cap would still hold but the test
    # would not exercise the concurrent acquisition path.
    barrier = threading.Barrier(_PARALLEL_REQUESTS)

    def _fire_one(_index: int) -> int:
        """Fire one POST /imagegen request and return its HTTP status.

        Args:
            _index: Worker index (unused; ``pool.map`` passes one per call).
        """
        barrier.wait()
        resp = c.post(
            f"/v1/personas/{pid}/imagegen",
            json={"prompt": "a red bicycle", "size": "1024x1024", "count": 1},
            headers=_auth(uid_a),
        )
        return resp.status_code

    with ThreadPoolExecutor(max_workers=_PARALLEL_REQUESTS) as pool:
        statuses = list(pool.map(_fire_one, range(_PARALLEL_REQUESTS)))

    # ------------------------------------------------------------------
    # HTTP-status invariant: exactly 1 × 201, exactly 9 × 429.
    # ------------------------------------------------------------------
    successes = [s for s in statuses if s == 201]
    capped = [s for s in statuses if s == 429]
    other = [s for s in statuses if s not in (201, 429)]
    assert len(successes) == 1, (
        f"expected exactly 1 successful 201, got {len(successes)};"
        f" full status distribution: {statuses}"
    )
    assert len(capped) == _PARALLEL_REQUESTS - 1, (
        f"expected exactly {_PARALLEL_REQUESTS - 1} concurrency-capped 429s,"
        f" got {len(capped)}; full status distribution: {statuses}"
    )
    assert other == [], f"expected no other status codes than 201/429, got unexpected: {other}"

    # ------------------------------------------------------------------
    # Backend-call-count invariant: exactly 1 request reached the
    # backend. This is the structural assertion against the
    # denial-of-wallet amplifier: even if a future code change broke
    # the credits ledger, this assertion alone proves the cap held.
    # ------------------------------------------------------------------
    assert slow_backend.call_count == 1, (
        f"expected exactly 1 backend call (the cap holder), got"
        f" {slow_backend.call_count} — the concurrency cap is broken"
    )

    # ------------------------------------------------------------------
    # Credits-ledger invariant (Spec M3, T3a): the ONE successful request
    # pre-deducts the per-image CEILING (50) then TRUES-UP to the real cost.
    # The slow backend reports no usage → unpriced → the charge falls to the
    # floor (1), so the true-up refunds the 49-credit overage: a [-50, +49]
    # pair, net -1. The nine concurrency-capped requests write NOTHING (the cap
    # raises before the pre-deduct). Without the cap, parallel pre-deducts would
    # land; this pair proves exactly one request billed.
    # ------------------------------------------------------------------
    deltas = _tx_deltas(su, uid_a)
    assert deltas == [-_IMAGE_CEILING, _IMAGE_CEILING - _REAL_CHARGE_FLOOR], (
        f"expected the ceiling pre-deduct + true-up refund pair"
        f" [-{_IMAGE_CEILING}, {_IMAGE_CEILING - _REAL_CHARGE_FLOOR}] (one success,"
        f" nine concurrency-capped), got {deltas}"
    )

    # Balance: exactly one net real charge (the floor) landed.
    final_balance = _balance(su, uid_a)
    assert final_balance == start_balance - _REAL_CHARGE_FLOOR, (
        f"expected final_balance == start_balance - {_REAL_CHARGE_FLOOR}"
        f" (start={start_balance}, final={final_balance}, delta={start_balance - final_balance})"
    )

    # ------------------------------------------------------------------
    # Audit-log invariant: exactly 1 × ``imagegen.create`` for this user.
    # The route only emits ``imagegen.create`` on the success path
    # (after :func:`persona_api.imagegen.service.generate` returns); the
    # nine concurrency-capped paths raise BEFORE the audit emission.
    # ------------------------------------------------------------------
    audit_count = _imagegen_audit_count(su, uid_a)
    assert audit_count == 1, f"expected exactly 1 imagegen.create audit row, got {audit_count}"

    # ------------------------------------------------------------------
    # Workspace-files invariant: exactly 1 image file on disk.
    # The bytes are deterministic (``_TINY_PNG``), so the content-
    # addressed write produces a single file at
    # ``uploads/<blake2b(_TINY_PNG)>.png``. Even if the cap leaked and
    # multiple successes happened with identical bytes, the
    # content-addressed write would collapse them — but the deduct +
    # audit + backend-call-count assertions above already rule that
    # branch out. This is the disk-side mirror of the binary invariant.
    # ------------------------------------------------------------------
    file_count = _count_workspace_image_files(workspace_root, uid_a, pid)
    assert file_count == 1, f"expected exactly 1 image file under uploads/, got {file_count}"
