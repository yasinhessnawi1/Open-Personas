"""The R6 crisis-detection encoder (Spec R6, R6-D-1) — the euphemistic/non-English net.

V11's R1 gate is **lexical**: reliable on explicit English self-harm phrasing, blind to
euphemistic, indirect, and non-English distress (the residual V11 owned and deferred —
V11-D-7). R6 adds a small fine-tuned **multilingual encoder** that catches what the
lexicon misses, and feeds V11's existing R1 machinery (it does NOT replace the lexical
gate — `classify_user_message` composes ``lexical ∪ encoder``, T4).

**Architecture (R6-D-1).** A frozen `sentence-transformers/paraphrase-multilingual-
MiniLM-L12-v2` body (50+ languages incl. ar/nb/sv/da/tr/ur; reused via the already-
shipped `SentenceTransformerEmbedder` — no new serving stack) + a tiny logistic-
regression head fitted on an authored few-shot set at load. This is SetFit's mechanism
minus the contrastive body-tune: SetFit's finding (≈8 ex/class ≈ RoBERTa-Large on 3k)
is what lets the labelled set stay small — a few dozen synthetic examples, no large
sensitive corpus (the reason V11 deferred R2). If the frozen body cannot clear the T5
bar, the escalation ladder is: contrastive body-tune (true SetFit) → gated-data-under-
DUA — in that order (R6-D-4).

The head emits a scalar crisis score in ``[0, 1]`` (``P(crisis)``) which T4 thresholds
twice: ``T_hard`` (high-precision → HARD bypass) and ``T_soft`` (recall-tuned → SOFT).

**In-process + fail-soft.** Lazy-loaded and thread-safe like the graph embedder; warm
up off the event loop (R6-D-2 / the voice-event-loop-starvation rule). Any failure to
load/fit/score is the CALLER's to absorb: `classify_user_message` treats an encoder
error as *no encoder signal* and falls back to the lexical + R0 floor (fail-soft→R0,
R6-D-5). This module raises :class:`CrisisEncoderError` on a genuine fault rather than
returning a silent 0.0, so the caller's ``except`` path is the single fail-soft seam.

**Bounded + never joined (R9-027).** The cold load is a NETWORK dependency when the HF
cache is cold, and huggingface_hub's own bounds are per-request only (its retry ladder
is unbounded in aggregate — see ``persona.stores.embedder``). So the warm-up (a) runs on
a dedicated **daemon thread**, never ``asyncio.to_thread``'s default executor, whose
stuck worker is exactly what ``loop.shutdown_default_executor`` (TestClient ``__exit__``
/ ``asyncio.run`` teardown) then joins without bound — the 2026-07-11 CI hang; and
(b) is awaited under a **hard deadline** (:func:`warmup_deadline_from_env`): on
deadline the boot logs a WARNING and serves lexical-only while the load continues in
the background and starts serving if it ever completes. Readiness/liveness never block
on the warm-up, and shutdown never joins the load thread.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import threading
from functools import lru_cache
from importlib import resources
from typing import TYPE_CHECKING, Final, Protocol, runtime_checkable

import yaml
from persona.logging import get_logger
from persona.stores import SentenceTransformerEmbedder

if TYPE_CHECKING:
    from collections.abc import Sequence

    from persona.stores import Embedder

__all__ = [
    "CRISIS_ENCODER_MODEL",
    "CRISIS_ENCODER_T_HARD",
    "CRISIS_ENCODER_T_SOFT",
    "CRISIS_ENCODER_VERSION",
    "DEFAULT_WARMUP_DEADLINE_S",
    "CrisisEncoder",
    "CrisisEncoderError",
    "CrisisScorer",
    "build_crisis_encoder",
    "start_crisis_encoder_warmup",
    "warmup_deadline_from_env",
]

#: Version of the crisis-encoder artifact (few-shot set + base model + head config +
#: the two thresholds), Spec 10 discipline. Bump on any change; the T5 gate re-runs per
#: version. The thresholds below are part of THIS artifact — they are calibrated
#: numbers, not free knobs (R6-D-3/4).
CRISIS_ENCODER_VERSION = "v1"

#: The frozen multilingual body (R6-D-1). 50+ languages incl. the v1 covered set. Served
#: on **torch**, reusing the shipped ``SentenceTransformerEmbedder`` (R6-D-1). The serving
#: process already loads torch for the bge memory embedder on every turn, so the crisis
#: body shares that resident runtime — an ONNX body would add a SECOND ~280 MB inference
#: runtime (onnxruntime) for no RSS win and torch never leaves the process (measured, T6).
#: ONNX is a RECORDED FUTURE LEVER (direct-onnxruntime, numerically identical fp32; the
#: eval record keeps the recipe + parity-guard) gated on real p95 pain OR the optimum↔
#: torch export incompatibility clearing — not warranted for v1.
CRISIS_ENCODER_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

#: ``T_soft`` — recall-tuned SOFT threshold (R6-D-3). Calibrated on the T2 eval suite
#: (T4): the knee where euphemistic recall 0.76 / non-English aggregate 0.88 meet a
#: false-SOFT floor of 3 structural controls; a false SOFT is low-harm (RCT PubMed
#: 15811983). ``score ≥ T_soft`` (and ``< T_hard``) → SOFT.
CRISIS_ENCODER_T_SOFT = 0.49

#: ``T_hard`` — high-precision HARD threshold (R6-D-3). A false HARD is a distinct
#: product harm (breaks character + resource dump on a benign chat), so it is tuned to
#: hold the false-BYPASS controls at 0 with margin: the top-scoring control on the T2
#: suite is 0.624, so 0.75 keeps a 0.126 precision margin. ``score ≥ T_hard`` → HARD.
CRISIS_ENCODER_T_HARD = 0.75

#: Default warm-up hard deadline (R9-027) — a HANG-GUARD, not a latency SLA. The
#: measured cold load is ~40 s on the prod VM class and seconds on a warm HF cache;
#: 120 s is ~3× the worst legitimate load, far below huggingface_hub's unbounded
#: aggregate retry ladder (per-request timeouts are 10 s, but 5 exponential-backoff
#: retries × many files add up without limit — the 2026-07-11 CI evidence). On
#: deadline the boot degrades to lexical-only (fail-soft→R0) and the load continues
#: on its daemon thread.
DEFAULT_WARMUP_DEADLINE_S: Final[float] = 120.0

_WARMUP_DEADLINE_ENV: Final[str] = "PERSONA_CRISIS_WARMUP_DEADLINE_S"

#: Name of the dedicated warm-up load thread — asserted by tests to prove the load
#: never rides the asyncio default executor (whose shutdown join was the CI hang).
WARMUP_THREAD_NAME: Final[str] = "crisis-encoder-warmup"

#: Bounded call-time acquire on the fit lock (R9-027). While the warm-up thread holds
#: the lock mid-load, a concurrent turn's ``score`` reaches ``_fit`` on one of the
#: safety-intercept pool's (non-daemon) workers; an UNBOUNDED acquire would wedge that
#: worker for as long as the load takes — and wedged pool workers are what interpreter
#: exit (``concurrent.futures``' atexit hook) then joins, hanging a real SIGTERM. 5 s
#: is far above any post-warm contention (the lock is only ever held long during the
#: one-time load) and far below the load itself, so the call degrades fast instead.
_FIT_LOCK_TIMEOUT_S: Final[float] = 5.0


def warmup_deadline_from_env() -> float:
    """The warm-up deadline from ``PERSONA_CRISIS_WARMUP_DEADLINE_S`` (R9-027).

    Unset/blank → :data:`DEFAULT_WARMUP_DEADLINE_S`. ``<= 0`` → no deadline (the
    pre-R9-027 unbounded wait — an explicit operator opt-out, e.g. a first boot on a
    known-slow link where lexical-only in the interim is unwanted). Malformed →
    default, with one WARNING (an operator typo must not change the hang-guard
    silently). Mirrors ``catalog_ttl_from_env``.
    """
    raw = os.environ.get(_WARMUP_DEADLINE_ENV, "").strip()
    if not raw:
        return DEFAULT_WARMUP_DEADLINE_S
    try:
        return float(raw)
    except ValueError:
        _log.warning(
            "malformed crisis warm-up deadline env value; using the default",
            env_var=_WARMUP_DEADLINE_ENV,
            value=raw,
            default_s=DEFAULT_WARMUP_DEADLINE_S,
        )
        return DEFAULT_WARMUP_DEADLINE_S


@runtime_checkable
class CrisisScorer(Protocol):
    """The scoring surface ``classify_user_message`` composes with (R6-D-3).

    Structural, so the composition depends on the *score*, not the concrete encoder —
    a fake scorer drives the composition tests without loading a model.
    """

    def score(self, text: str) -> float: ...


class _LogRegHead(Protocol):
    """The slice of sklearn's ``LogisticRegression`` this module uses (untyped dep).

    Keeps the fitted head out of ``Any`` — ``predict_proba`` returns one row per input,
    ``row[1]`` is ``P(crisis)``.
    """

    def predict_proba(self, x: list[list[float]]) -> list[list[float]]: ...


_log = get_logger("safety.crisis_encoder")

_TRAIN_PACKAGE = "persona_runtime.data"
_TRAIN_RESOURCE = "crisis_fewshot_train.yaml"


class CrisisEncoderError(RuntimeError):
    """A genuine encoder fault (load / fit / score). The caller fails soft to R0."""


def _normalise(text: str) -> str:
    """Lower-case + collapse whitespace — mirrors the lexical gate's normalisation."""
    return " ".join(text.lower().split())


def _load_fewshot(package: str, resource: str) -> tuple[list[str], list[int]]:
    """Load the authored few-shot set → (normalised texts, binary labels)."""
    raw = yaml.safe_load(resources.files(package).joinpath(resource).read_text("utf-8"))
    texts: list[str] = []
    labels: list[int] = []
    for row in raw["examples"]:
        texts.append(_normalise(str(row["text"])))
        labels.append(1 if row["label"] == "crisis" else 0)
    if not texts or len(set(labels)) < 2:
        msg = "few-shot set must carry both crisis and benign examples"
        raise CrisisEncoderError(msg)
    return texts, labels


class CrisisEncoder:
    """A frozen-body + few-shot-head crisis scorer (R6-D-1), lazy and thread-safe.

    Args:
        embedder: the frozen multilingual body. Defaults to a lazily-constructed
            :class:`SentenceTransformerEmbedder` over :data:`CRISIS_ENCODER_MODEL`
            (its own model instance — NOT the bge graph embedder). Injectable for tests
            and for a shared app-scoped instance.
        train_package / train_resource: the bundled few-shot set location (R6-D-4).
        random_state: fixed so the head fit is deterministic per (data, base model).
    """

    def __init__(
        self,
        *,
        embedder: Embedder | None = None,
        train_package: str = _TRAIN_PACKAGE,
        train_resource: str = _TRAIN_RESOURCE,
        random_state: int = 0,
    ) -> None:
        self.model_name = CRISIS_ENCODER_MODEL
        self._embedder = embedder
        self._train_package = train_package
        self._train_resource = train_resource
        self._random_state = random_state
        self._head: _LogRegHead | None = None  # fitted sklearn LogisticRegression
        self._fit_lock = threading.Lock()

    def _resolve_embedder(self) -> Embedder:
        if self._embedder is None:
            # Pinned to CPU, the codebase-wide convention (the bge default_embedder + the K9
            # cross-encoder do the same): ``device="auto"`` selects Apple MPS on a Mac, where
            # this lazy/threaded load (warmup runs on a dedicated daemon thread, R9-027)
            # intermittently raises "Cannot copy out of meta tensor". MiniLM-L12 encodes fast
            # on CPU (R6's measured ~85-90 ms onset already assumed it), so CPU is both robust
            # and fast enough.
            self._embedder = SentenceTransformerEmbedder(
                model_name=self.model_name, normalize=True, device="cpu"
            )
        return self._embedder

    def _fit(self) -> _LogRegHead:
        """Lazily fit the head over the frozen-body embeddings (once, thread-safe).

        The acquire is BOUNDED (R9-027): while the warm-up thread holds the lock
        mid-load, a concurrent call-time ``score`` fails fast into the caller's
        fail-soft→R0 seam (:class:`CrisisEncoderError`) instead of wedging its
        pool worker on the lock for as long as the load takes — a wedged
        non-daemon pool worker is exactly what a real SIGTERM's interpreter-exit
        join then hangs on. Once the load completes, the fast path serves.
        """
        if self._head is not None:
            return self._head
        if not self._fit_lock.acquire(timeout=_FIT_LOCK_TIMEOUT_S):
            msg = (
                "crisis-encoder is still loading/fitting on another thread; "
                "this call degrades to lexical-only until the warm-up completes"
            )
            raise CrisisEncoderError(msg)
        try:
            if self._head is not None:
                return self._head
            try:
                from sklearn.linear_model import LogisticRegression

                texts, labels = _load_fewshot(self._train_package, self._train_resource)
                vectors = self._resolve_embedder().encode(texts)
                head: _LogRegHead = LogisticRegression(
                    random_state=self._random_state,
                    max_iter=1000,
                    class_weight="balanced",  # lean to recall; T_hard holds precision (R6-D-3)
                ).fit(vectors, labels)
            except CrisisEncoderError:
                raise
            except Exception as exc:  # noqa: BLE001 — surface as one fault type for fail-soft
                msg = f"crisis-encoder fit failed: {exc}"
                raise CrisisEncoderError(msg) from exc
            _log.info(
                "crisis-encoder fitted model={model} n_examples={n} version={ver}",
                model=self.model_name,
                n=len(texts),
                ver=CRISIS_ENCODER_VERSION,
            )
            self._head = head
            return head
        finally:
            self._fit_lock.release()

    def warmup(self) -> None:
        """Pay the cold model load + head fit once, OFF the event loop (R6-D-2).

        Wired at runner start (like the bge warm-up) so the first turn never pays the
        cold load on the critical path. Best-effort: raises :class:`CrisisEncoderError`
        the caller may log-and-ignore.
        """
        self._fit()

    def score(self, text: str) -> float:
        """The crisis score ``P(crisis) ∈ [0, 1]`` for one message (R6-D-1/3).

        Empty/whitespace → ``0.0`` (no signal). A load/fit/score fault raises
        :class:`CrisisEncoderError`; the caller (``classify_user_message``) absorbs it
        into fail-soft→R0. NEVER a network call — a single local forward pass + a
        logistic-regression evaluation (~1–5 ms warm, R6-D-2).
        """
        normalised = _normalise(text)
        if not normalised:
            return 0.0
        head = self._fit()
        try:
            vectors = self._resolve_embedder().encode([normalised])
            proba = head.predict_proba(vectors)[0][1]
        except Exception as exc:  # noqa: BLE001 — one fault type for the fail-soft seam
            msg = f"crisis-encoder score failed: {exc}"
            raise CrisisEncoderError(msg) from exc
        return float(proba)

    def score_batch(self, texts: Sequence[str]) -> list[float]:
        """Batch :meth:`score` (used by the eval harness; one forward pass)."""
        normalised = [_normalise(t) for t in texts]
        head = self._fit()
        try:
            vectors = self._resolve_embedder().encode([t if t else " " for t in normalised])
            probas = head.predict_proba(vectors)
        except Exception as exc:  # noqa: BLE001 — one fault type for the fail-soft seam
            msg = f"crisis-encoder batch score failed: {exc}"
            raise CrisisEncoderError(msg) from exc
        return [0.0 if not t else float(row[1]) for t, row in zip(normalised, probas, strict=True)]


@lru_cache(maxsize=1)
def build_crisis_encoder() -> CrisisEncoder:
    """The app-scoped crisis encoder for a composition root (RuntimeFactory / voice runner).

    Lazy — the ~470 MB model + head fit are paid at :meth:`CrisisEncoder.warmup` (boot,
    off-loop) or, failing that, on the first :meth:`CrisisEncoder.score`. One instance per
    process (``lru_cache`` — enforced, not aspirational), injected into every path so there
    is ONE classifier (R6-D-3). Without the cache, every app build loads its own ~470 MB
    model — N test-suite app builds in one pytest process accumulated N loads and corrupted
    torch's meta-device init (the "Cannot copy out of meta tensor" cascade). Tests that need
    an isolated instance construct :class:`CrisisEncoder` directly.
    """
    return CrisisEncoder()


async def _run_warmup(encoder: CrisisEncoder, *, deadline_s: float | None = None) -> None:
    """Drive :meth:`CrisisEncoder.warmup` on a dedicated daemon thread, deadline-bounded.

    R9-027 mechanics (see the module docstring for the WHY):

    * The load runs on a plain ``threading.Thread(daemon=True)`` — deliberately NOT
      ``asyncio.to_thread`` (whose default-executor worker gets an unbounded join from
      ``loop.shutdown_default_executor`` at TestClient exit / ``asyncio.run`` teardown)
      and NOT a ``ThreadPoolExecutor`` (whose non-daemon workers get joined by
      ``concurrent.futures``' atexit hook on real SIGTERM). Nothing ever joins this
      thread; a stuck load is abandoned, never waited on.
    * The awaiting side is bounded by ``deadline_s`` (default:
      :func:`warmup_deadline_from_env`). On deadline: one WARNING, then the boot
      proceeds lexical-only. The thread keeps loading in the background — if it ever
      completes, ``_fit``'s fast path starts serving encoder verdicts (late-arrival
      recovery for free).
    * A load that finishes AFTER the event loop closed (test client exited mid-load)
      finds ``call_soon_threadsafe`` raising ``RuntimeError`` — swallowed; there is
      nobody left to notify.
    """
    deadline = warmup_deadline_from_env() if deadline_s is None else deadline_s
    loop = asyncio.get_running_loop()
    done: asyncio.Future[None] = loop.create_future()

    def _resolve(exc: BaseException | None) -> None:
        # Runs ON the event loop. After a deadline, ``wait_for`` has cancelled
        # ``done`` — setting a result then would raise InvalidStateError.
        if done.done():
            return
        if exc is None:
            done.set_result(None)
        else:
            done.set_exception(exc)

    def _target() -> None:
        err: BaseException | None
        try:
            encoder.warmup()
            err = None
        except BaseException as exc:  # noqa: BLE001 — marshalled to the awaiting task
            err = exc
        # RuntimeError ⇒ the loop already closed (test client exited mid-load;
        # process shutting down) — nobody left to notify; the daemon thread ends.
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(_resolve, err)

    threading.Thread(target=_target, name=WARMUP_THREAD_NAME, daemon=True).start()
    try:
        if deadline > 0:
            await asyncio.wait_for(done, timeout=deadline)
        else:
            await done
    except TimeoutError:
        _log.warning(
            "crisis-encoder warm-up exceeded its {deadline_s}s deadline; serving "
            "lexical-only (fail-soft→R0) while the load continues on its daemon "
            "thread — encoder verdicts resume automatically if it completes "
            "(model={model})",
            deadline_s=deadline,
            model=encoder.model_name,
        )
        return
    except asyncio.CancelledError:
        # Shutdown cancelled the warm task (app.py's lifespan finally). The daemon
        # thread is abandoned — never joined — so shutdown cannot hang on it.
        raise
    except Exception as exc:  # noqa: BLE001 — best-effort; classify fails soft to lexical
        _log.warning(
            "crisis-encoder warm-up failed; lexical-only until it succeeds (error={error})",
            error=str(exc),
        )
        return
    _log.info("crisis-encoder warm (version={ver})", ver=CRISIS_ENCODER_VERSION)


def start_crisis_encoder_warmup(
    encoder: CrisisEncoder, *, deadline_s: float | None = None
) -> asyncio.Task[None]:
    """Kick the encoder cold-load OFF the event loop at boot (R6-D-2 / T6 / T8).

    The composition root calls this at startup so the FIRST user never pays the ~40 s
    load (the built-but-inert failure class). Best-effort and non-blocking: the boot
    proceeds immediately; during the warm window a turn's encoder score fails/times
    out fast (the 2 s hang-guard + the bounded ``_fit`` acquire) and the turn runs
    lexical-only (fail-soft→R0), so the warm window is degraded-but-safe, never
    blocked. Mirrors the bge ``start_embedder_warmup`` precedent.

    R9-027: the load rides a dedicated daemon thread under a hard deadline
    (``deadline_s``; default ``PERSONA_CRISIS_WARMUP_DEADLINE_S`` → 120 s) — see
    :func:`_run_warmup`. Readiness/liveness never block on this task, and neither
    TestClient ``__exit__`` nor a real SIGTERM ever joins the load thread; callers
    should still ``cancel()`` the returned task at shutdown (the api lifespan does)
    so the awaiting side ends promptly.
    """
    return asyncio.create_task(_run_warmup(encoder, deadline_s=deadline_s))
