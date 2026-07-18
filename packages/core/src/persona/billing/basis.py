"""The ``cost_basis`` provenance vocabulary (Spec M3, D-M3-12).

Moved to persona-core (from ``persona_runtime.cost``, M2) so the M3 billing
seam — which persona-voice imports directly and persona-api injects around —
has one authoritative home for the basis vocabulary without importing the
runtime. ``persona_runtime.cost`` re-exports :data:`CostBasis` for its existing
importers (the loop, the ``persona.credits`` mirror, the ``/v1/me/usage`` route),
so nothing downstream changes.

Vocabulary (persisted on ``turn_logs.cost_basis`` and, from M3, on
``credit_transactions.cost_basis``):

* ``"actual_openrouter"`` — the response's own ``usage.cost`` (what we paid).
* ``"estimate_static"`` — static per-provider table hit (vendor-published).
* ``"estimate_catalog"`` — OpenRouter catalog hit (derived, best-effort).
* ``"provider_meter"`` — a per-unit provider meter (voice STT/TTS per
  streamed-second / character), summed to the turn/tick (M3, T6).
* ``"infra_flat"`` — a zero-provider-cost surface charged the infra flat rate
  only (embeddings, connectors, MCP, sandbox, LiveKit transport) (M3, D-M3-6).
* ``"unpriced"`` — no data; ``cost_cents`` recorded as ``0.0``.
"""

from __future__ import annotations

from typing import Literal

__all__ = ["CostBasis"]

CostBasis = Literal[
    "actual_openrouter",
    "estimate_static",
    "estimate_catalog",
    "provider_meter",
    "infra_flat",
    "unpriced",
]
