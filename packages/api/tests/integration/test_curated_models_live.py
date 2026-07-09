"""Live-catalog validation for the M1 T5 curated model shortlist (external, manual).

``@pytest.mark.external`` — hits the REAL OpenRouter catalog (no mock, no fake),
never runs in CI. This is the durable form of the task-5 brief's build-time
instruction ("run the T5 integration check against the LIVE catalog once
locally; drop/replace any id that doesn't validate") — a re-runnable regression
check, so a future catalog change (an id retired, renamed, or re-priced to the
R9-018 ``-1`` sentinel) is caught by re-running this file instead of rediscovered
by a confused user staring at a picker missing an entry.

Skips when ``PERSONA_OPENROUTER_API_KEY`` is unset — the same "skip when the
required API key is unset" idiom as ``test_authoring_corpus_external.py``.

Run:  PERSONA_OPENROUTER_API_KEY=sk-or-... uv run pytest -m external \
        packages/api/tests/integration/test_curated_models_live.py -s
"""

from __future__ import annotations

import os

import pytest
from persona.backends.metadata.openrouter_resolver import OpenRouterModelMetadataResolver
from persona.backends.openrouter_catalog import OpenRouterCatalogClient
from persona_api.services.model_catalog_service import CURATED_MODEL_IDS

pytestmark = pytest.mark.external


def test_every_curated_id_resolves_against_the_live_catalog() -> None:
    """Each :data:`CURATED_MODEL_IDS` entry must exist, be chat-capable, and price-valid, live.

    Mirrors exactly what
    :func:`persona_api.services.model_catalog_service.list_models` checks for
    ``scope="recommended"``: present in the live catalog, "text" in
    ``architecture.output_modalities`` (chat-capable), and
    :meth:`OpenRouterModelMetadataResolver.resolve` returns metadata (excludes
    the R9-018 ``-1`` sentinel class and any other unparseable-pricing entry).
    """
    api_key = os.environ.get("PERSONA_OPENROUTER_API_KEY", "").strip()
    if not api_key:
        pytest.skip("PERSONA_OPENROUTER_API_KEY not set; skipping live catalog validation")

    client = OpenRouterCatalogClient(api_key)
    try:
        entries = client.list_models()
    finally:
        client.close()
    resolver = OpenRouterModelMetadataResolver(client)

    by_id = {entry.id: entry for entry in entries}
    failures: list[str] = []
    for model_id in CURATED_MODEL_IDS:
        entry = by_id.get(model_id)
        if entry is None:
            failures.append(f"{model_id}: not found in live catalog")
            continue
        if "text" not in entry.architecture.output_modalities:
            failures.append(
                f"{model_id}: not chat-capable "
                f"(output_modalities={entry.architecture.output_modalities})"
            )
            continue
        if resolver.resolve(model_id) is None:
            failures.append(
                f"{model_id}: resolver rejected pricing "
                "(deprecated / -1 sentinel / otherwise unresolvable)"
            )

    assert not failures, "CURATED_MODEL_IDS drifted from the live catalog:\n" + "\n".join(failures)
