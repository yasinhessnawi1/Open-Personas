"""Test-only embedder.

Deterministic, normalised, SHA-256-derived. Used wherever a test needs
an :class:`persona.stores.Embedder`-compatible object without paying the
~3s sentence-transformers cold start. Lives outside ``conftest.py`` so
tests can import it as a regular module.
"""

from __future__ import annotations

import hashlib
import math
import struct
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["HashEmbedder"]


class HashEmbedder:
    """Same text → same vector. Always-L2-normalised. Fixed-dim (32)."""

    model_name: str = "test-hash-embedder"
    dimension: int = 32

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            floats = list(struct.unpack("8f", digest))
            # Raw IEEE decode of random bytes can yield NaN/Infinity, which
            # Chroma rejects (surfaced by Spec K8's uuidv7-randomised test
            # texts — pre-K8 tests used fixed texts that happened to decode
            # finite). Deterministic finite fallback per lane; unchanged for
            # every text that already decoded finite.
            floats = [
                x if math.isfinite(x) else (digest[i * 4] + 1) / 255.0 for i, x in enumerate(floats)
            ]
            full = (floats * 4)[: self.dimension]
            norm = math.sqrt(sum(x * x for x in full)) or 1.0
            out.append([x / norm for x in full])
        return out
