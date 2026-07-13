"""R9-027 — hermetic boot: the real lifespan boots OFFLINE, degrades, never hangs.

Pins the 2026-07-11 incident class dead at the app seam. The incident: the
app-factory test booted the real lifespan, whose crisis-encoder warmup
DOWNLOADED its sentence-transformers model from huggingface.co on a cold CI
runner during an HF outage — huggingface_hub's retry ladder ate the 120 s
pytest timeout, and TestClient shutdown then joined the stuck executor thread
(``loop.shutdown_default_executor`` → ``thread.join``, unbounded).

Four pins:

* The suite-wide offline env (set at conftest import — see conftest.py) is
  ACTIVE, and — decisively — huggingface_hub's IMPORT-TIME constants actually
  froze offline (the env var alone proves nothing if some earlier import beat
  the conftest; this catches any import-order regression re-opening the net).
* A REAL-lifespan community boot with a deliberately COLD model cache
  completes offline within a bound: the crisis-encoder warmup fails fast
  (WARNING) and degrades to lexical-only instead of downloading/hanging, and
  the app serves requests throughout.
* A deliberately STUCK loader (the outage shape: a load that never returns)
  cannot hold shutdown: ``TestClient.__exit__`` returns within a small bound —
  the load rides a daemon thread nothing ever joins (R9-027 commit 2).
"""

from __future__ import annotations

import os
import threading
import time
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from loguru import logger as _loguru_logger
from persona_api.app import create_app
from persona_api.config import APIConfig, Edition
from persona_runtime.crisis_encoder import CrisisEncoder

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path

    from persona.stores.embedder import Embedder


@pytest.fixture
def loguru_capture() -> Iterator[list[str]]:
    """Loguru sink capturing every ≥WARNING line (persona.logging wraps loguru,
    so stdlib ``caplog`` sees nothing — the test_m2_multiround_usage.py pattern)."""
    captured: list[str] = []
    sink_id = _loguru_logger.add(lambda msg: captured.append(str(msg)), level="WARNING")
    try:
        yield captured
    finally:
        _loguru_logger.remove(sink_id)


def _community_config(tmp_path: Path) -> APIConfig:
    """The zero-infra community config (the test_api_community_boot shape)."""
    return APIConfig(
        edition=Edition.community,
        community_db_path=tmp_path / "community.db",
        community_memory_path=tmp_path / "chroma",
        workspace_root=tmp_path / "work",
        audit_root=str(tmp_path / "audit"),
    )


def _prepare_offline_boot(monkeypatch: pytest.MonkeyPatch, embedder: Embedder) -> None:
    """Common arrangement for the real-lifespan boots below.

    No DSN (pure community boot); the app's bge embedder is the torch-free hash
    fake (create-time indexing must not load torch in the unit job — the
    community-boot precedent); the ENCODER stays REAL so the warmup exercises
    the genuine load chain (warmup → _fit → SentenceTransformerEmbedder._load →
    huggingface_hub), which is exactly the incident path.
    """
    from persona_api.services import persona_service

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)
    monkeypatch.setattr(persona_service, "default_embedder", lambda *_a, **_k: embedder)
    # The crisis path must be genuinely armed (an operator shell with the
    # encoder toggled off would silently skip the surface under test).
    monkeypatch.setenv("PERSONA_SAFETY_ENCODER_ENABLED", "true")


class TestOfflineEnvIsPinned:
    def test_suite_env_pins_hf_offline(self) -> None:
        # The conftest sets these at module import, before anything can import
        # huggingface_hub. setdefault semantics: an operator's explicit value wins.
        assert os.environ.get("HF_HUB_OFFLINE") == "1"
        assert os.environ.get("TRANSFORMERS_OFFLINE") == "1"

    def test_huggingface_hub_import_time_constants_froze_offline(self) -> None:
        # THE mechanism, not the env var: huggingface_hub reads the vars ONCE at
        # import and mounts an OfflineAdapter on every hub session. If a future
        # import-order change lets huggingface_hub import before the conftest
        # export, the env test above still passes while the network silently
        # re-opens — this one reds.
        constants = pytest.importorskip(
            "huggingface_hub.constants", reason="huggingface_hub not installed"
        )
        assert constants.HF_HUB_OFFLINE is True


class TestOfflineBootDegrades:
    def test_cold_cache_boot_completes_offline_serves_and_degrades(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        embedder: Embedder,
        loguru_capture: list[str],
    ) -> None:
        """The real lifespan + a COLD model cache: boot must complete offline.

        ``SENTENCE_TRANSFORMERS_HOME`` → an empty tmp dir makes the crisis
        model's cache cold DETERMINISTICALLY (sentence-transformers reads that
        var at construction time, so this works even though huggingface_hub's
        own HF_HOME froze at import; a dev machine's warm ~/.cache is bypassed).
        Offline + cold cache ⇒ the load raises fast — the boot must log the
        warm-up WARNING, stay unfitted, and SERVE, all inside the bound the
        pre-fix retry ladder blew (>120 s in CI).
        """
        _prepare_offline_boot(monkeypatch, embedder)
        monkeypatch.setenv("SENTENCE_TRANSFORMERS_HOME", str(tmp_path / "cold-st-cache"))

        import persona_runtime.crisis_encoder as crisis_mod

        # Order-independence: the lru-cached shared instance may already be
        # fitted by an earlier app boot in this process (on a dev machine with
        # a warm default cache). A FRESH real CrisisEncoder keeps the genuine
        # load chain while making the cold-cache outcome deterministic.
        fresh = CrisisEncoder()
        monkeypatch.setattr(crisis_mod, "build_crisis_encoder", lambda: fresh)

        started = time.monotonic()
        app = create_app(_community_config(tmp_path))
        with TestClient(app) as client:
            resp = client.get("/openapi.json")
            assert resp.status_code == 200, resp.text
            # The warmup runs in the background — wait (bounded) for its verdict.
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline:
                if any("crisis-encoder warm-up" in line for line in loguru_capture):
                    break
                time.sleep(0.05)
            assert any("crisis-encoder warm-up failed" in line for line in loguru_capture), (
                f"expected the offline cold-cache load to fail fast and WARN; got {loguru_capture}"
            )
            assert fresh._head is None  # noqa: SLF001 — degraded: unfitted, lexical-only
            # Degraded ≠ down: the app keeps serving while lexical-only.
            assert client.get("/openapi.json").status_code == 200
        elapsed = time.monotonic() - started
        assert elapsed < 60.0, f"offline boot+shutdown took {elapsed:.1f}s — retry-ladder class"

    def test_shutdown_returns_within_bound_with_a_stuck_loader(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, embedder: Embedder
    ) -> None:
        """A load that NEVER returns (the outage shape) cannot hold shutdown.

        Pre-fix: the warmup rode ``asyncio.to_thread`` → TestClient exit ran
        ``loop.shutdown_default_executor`` → joined the stuck worker without
        bound (the CI hang's stack). Post-fix the load rides a daemon thread
        nothing joins, and the lifespan cancels the awaiting task — the whole
        boot→serve→shutdown arc must finish well inside the old failure mode.
        """
        _prepare_offline_boot(monkeypatch, embedder)

        release = threading.Event()
        entered = threading.Event()

        class _StuckBody:
            """An embedder body that blocks like a wedged download."""

            model_name = "stuck-stub"

            @property
            def dimension(self) -> int:
                return 3

            def encode(self, texts: Sequence[str]) -> list[list[float]]:
                entered.set()
                if not release.wait(timeout=300.0):  # pragma: no cover - safety ceiling
                    msg = "stuck body never released"
                    raise RuntimeError(msg)
                return [[0.0, 0.0, 1.0] for _ in texts]

        import persona_runtime.crisis_encoder as crisis_mod

        stuck = CrisisEncoder(embedder=_StuckBody())
        monkeypatch.setattr(crisis_mod, "build_crisis_encoder", lambda: stuck)

        started = time.monotonic()
        try:
            app = create_app(_community_config(tmp_path))
            with TestClient(app) as client:
                assert client.get("/openapi.json").status_code == 200
                # The loader must genuinely be mid-"download" when we shut down —
                # otherwise this proves nothing about a stuck join.
                assert entered.wait(timeout=15.0), "the warmup loader never started"
            elapsed = time.monotonic() - started
            assert elapsed < 30.0, (
                f"boot+serve+shutdown took {elapsed:.1f}s with a stuck loader — "
                "shutdown joined the load (the 2026-07-11 CI hang class)"
            )
        finally:
            release.set()  # let the leaked daemon thread exit cleanly
