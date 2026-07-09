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
"""

from __future__ import annotations

import asyncio
import threading
from functools import lru_cache
from importlib import resources
from typing import TYPE_CHECKING, Protocol, runtime_checkable

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
    "CrisisEncoder",
    "CrisisEncoderError",
    "CrisisScorer",
    "build_crisis_encoder",
    "start_crisis_encoder_warmup",
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
            # this lazy/threaded load (warmup runs via asyncio.to_thread) intermittently raises
            # "Cannot copy out of meta tensor". MiniLM-L12 encodes fast on CPU (R6's measured
            # ~85-90 ms onset already assumed it), so CPU is both robust and fast enough.
            self._embedder = SentenceTransformerEmbedder(
                model_name=self.model_name, normalize=True, device="cpu"
            )
        return self._embedder

    def _fit(self) -> _LogRegHead:
        """Lazily fit the head over the frozen-body embeddings (once, thread-safe)."""
        if self._head is not None:
            return self._head
        with self._fit_lock:
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


async def _run_warmup(encoder: CrisisEncoder) -> None:
    try:
        await asyncio.to_thread(encoder.warmup)
        _log.info("crisis-encoder warm (version={ver})", ver=CRISIS_ENCODER_VERSION)
    except Exception as exc:  # noqa: BLE001 — best-effort; classify fails soft to lexical
        _log.warning(
            "crisis-encoder warm-up failed; lexical-only until it succeeds (error={error})",
            error=str(exc),
        )


def start_crisis_encoder_warmup(encoder: CrisisEncoder) -> asyncio.Task[None]:
    """Kick the encoder cold-load OFF the event loop at boot (R6-D-2 / T6 / T8).

    The composition root calls this at startup so the FIRST user never pays the ~40 s
    load (the built-but-inert failure class). Best-effort and non-blocking: the boot
    proceeds immediately; during the warm window a turn's encoder score times out (the
    2 s hang-guard) and the turn runs lexical-only (fail-soft→R0), so the warm window is
    degraded-but-safe, never blocked. Mirrors the bge ``start_embedder_warmup`` precedent.
    """
    return asyncio.create_task(_run_warmup(encoder))
