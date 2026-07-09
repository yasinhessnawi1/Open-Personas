"""Unit tests for the M1 T5 model catalog service — fake client, no network.

Drives :func:`persona_api.services.model_catalog_service.list_models` directly
with a duck-typed fake standing in for
:class:`~persona.backends.openrouter_catalog.OpenRouterCatalogClient` (this
suite never touches the network — the real client is only exercised by the
``@pytest.mark.external``, key-gated ``test_curated_models_live.py``, which
never runs in CI).

Covers: recommended = curated ∩ valid in curated order; all = every valid entry
(curated-flagged); the R9-018 ``-1`` sentinel pricing exclusion; the chat-capable
(text-output) gate; the price-per-1M conversion pinned against a hand-computed
value; and the fail-open paths (no client configured, fetch raises).
"""

from __future__ import annotations

import pytest
from persona.backends.errors import OpenRouterCatalogError
from persona.backends.openrouter_catalog import (
    OpenRouterArchitecture,
    OpenRouterModelEntry,
    OpenRouterPricing,
)
from persona_api.services import model_catalog_service as svc

_CURATED_ID = svc.CURATED_MODEL_IDS[0]  # "anthropic/claude-sonnet-4.6"


def _entry(
    model_id: str,
    *,
    prompt: str,
    completion: str,
    name: str = "",
    context_length: int | None = 128_000,
    tools: bool = True,
    text_output: bool = True,
) -> OpenRouterModelEntry:
    """Build a catalog entry directly (no JSON round-trip needed for a fake)."""
    return OpenRouterModelEntry(
        id=model_id,
        name=name,
        context_length=context_length,
        pricing=OpenRouterPricing(prompt=prompt, completion=completion),
        architecture=OpenRouterArchitecture(
            output_modalities=("text",) if text_output else ("embedding",)
        ),
        supported_parameters=("tools",) if tools else (),
    )


class _FakeCatalogClient:
    """Duck-typed :class:`OpenRouterCatalogClient` stand-in — no network."""

    def __init__(self, entries: tuple[OpenRouterModelEntry, ...]) -> None:
        self._entries = entries

    def list_models(self, *, force_refresh: bool = False) -> tuple[OpenRouterModelEntry, ...]:
        del force_refresh
        return self._entries


class _RaisingCatalogClient:
    """Duck-typed client whose ``list_models`` always raises (fetch-failure path)."""

    def list_models(self, *, force_refresh: bool = False) -> tuple[OpenRouterModelEntry, ...]:
        del force_refresh
        raise OpenRouterCatalogError(
            "boom", context={"provider": "openrouter", "reason": "timeout"}
        )


# -- fixture entries mirroring the brief's Step-1 scenario -------------------
#
# entry_curated_valid: curated id, real known Claude Sonnet 4.6 pricing
#   ($3/Mtok in, $15/Mtok out) — doubles as the price-conversion cross-check.
# entry_sentinel: NOT curated, the R9-018 "-1" sentinel class (OpenRouter's own
#   meta-router aliases use this, e.g. "openrouter/auto").
# entry_noncurated_valid: NOT curated, valid pricing.

_ENTRY_CURATED_VALID = _entry(
    _CURATED_ID, prompt="0.000003", completion="0.000015", name="Anthropic: Claude Sonnet 4.6"
)
_ENTRY_SENTINEL = _entry("openrouter/auto", prompt="-1", completion="-1", name="Auto Router")
_ENTRY_NONCURATED_VALID = _entry(
    "some-provider/small-model", prompt="0.0000005", completion="0.000001"
)

_THREE_ENTRIES = (_ENTRY_CURATED_VALID, _ENTRY_SENTINEL, _ENTRY_NONCURATED_VALID)


def test_recommended_is_curated_intersect_valid() -> None:
    """recommended = curated ∩ valid — the sentinel and the non-curated entry are excluded."""
    result = svc.list_models(scope="recommended", client=_FakeCatalogClient(_THREE_ENTRIES))
    assert result.source == "openrouter"
    assert result.stale is False
    assert [m.id for m in result.models] == [_CURATED_ID]
    assert result.models[0].recommended is True


def test_recommended_preserves_curated_order() -> None:
    """Multiple curated hits come back in CURATED_MODEL_IDS order, not catalog order."""
    second_curated = svc.CURATED_MODEL_IDS[1]
    third_curated = svc.CURATED_MODEL_IDS[2]
    entries = (
        # Deliberately fed in the REVERSE of curated order.
        _entry(third_curated, prompt="0.000001", completion="0.00001"),
        _entry(second_curated, prompt="0.000001", completion="0.00001"),
        _entry(_CURATED_ID, prompt="0.000003", completion="0.000015"),
    )
    result = svc.list_models(scope="recommended", client=_FakeCatalogClient(entries))
    assert [m.id for m in result.models] == [_CURATED_ID, second_curated, third_curated]


def test_all_includes_curated_and_noncurated_valid_but_not_sentinel() -> None:
    """all = every valid entry (curated + non-curated); the sentinel-priced entry is dropped."""
    result = svc.list_models(scope="all", client=_FakeCatalogClient(_THREE_ENTRIES))
    assert result.stale is False
    ids = {m.id for m in result.models}
    assert ids == {_CURATED_ID, "some-provider/small-model"}

    by_id = {m.id: m for m in result.models}
    assert by_id[_CURATED_ID].recommended is True
    assert by_id["some-provider/small-model"].recommended is False


def test_non_chat_capable_entry_is_excluded_even_if_curated_and_valid() -> None:
    """The chat-capable (text-output) gate excludes an entry regardless of pricing/curation."""
    entry = _entry(_CURATED_ID, prompt="0.000003", completion="0.000015", text_output=False)
    result = svc.list_models(scope="recommended", client=_FakeCatalogClient((entry,)))
    assert result.models == ()
    assert result.stale is False  # a successful fetch that filtered everything is NOT stale


def test_label_falls_back_to_id_when_catalog_name_is_blank() -> None:
    entry = _entry("some-provider/small-model", prompt="0.0000005", completion="0.000001", name="")
    result = svc.list_models(scope="all", client=_FakeCatalogClient((entry,)))
    assert result.models[0].label == "some-provider/small-model"
    assert result.models[0].provider == "some-provider"


def test_price_conversion_pinned_against_known_value() -> None:
    """1 cent per 1k tokens == $0.01 per 1k == $10 per 1M — the ONE conversion point."""
    assert svc._usd_per_1m(1.0) == 10.0  # noqa: SLF001 — pinning the private helper directly


def test_price_conversion_matches_a_real_known_price_shape() -> None:
    """Cross-check: Claude Sonnet 4.6's real pricing round-trips to $3 in / $15 out per 1M."""
    result = svc.list_models(
        scope="recommended", client=_FakeCatalogClient((_ENTRY_CURATED_VALID,))
    )
    (model,) = result.models
    assert model.input_price_per_1m == pytest.approx(3.0)
    assert model.output_price_per_1m == pytest.approx(15.0)


def test_context_length_and_tools_supported_are_surfaced() -> None:
    entry = _entry(
        "some-provider/small-model",
        prompt="0.0000005",
        completion="0.000001",
        context_length=32_000,
        tools=True,
    )
    result = svc.list_models(scope="all", client=_FakeCatalogClient((entry,)))
    assert result.models[0].context_length == 32_000
    assert result.models[0].tools_supported is True


def test_fetch_failure_is_stale_empty_not_raised() -> None:
    """A catalog fetch failure fails OPEN: stale=True, empty models, no exception."""
    result = svc.list_models(scope="recommended", client=_RaisingCatalogClient())
    assert result.stale is True
    assert result.models == ()
    assert result.source == "openrouter"

    all_result = svc.list_models(scope="all", client=_RaisingCatalogClient())
    assert all_result.stale is True
    assert all_result.models == ()


def test_no_client_configured_is_stale_empty_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    """No PERSONA_OPENROUTER_API_KEY -> the module-scoped client is None -> fail-open."""
    monkeypatch.delenv(svc._API_KEY_ENV, raising=False)  # noqa: SLF001 — env-driven singleton
    svc._default_client.cache_clear()  # noqa: SLF001 — force env re-read
    try:
        result = svc.list_models(scope="recommended")
        assert result.stale is True
        assert result.models == ()
    finally:
        svc._default_client.cache_clear()  # noqa: SLF001 — don't leak into other tests


def test_curated_model_ids_are_unique_and_nonempty() -> None:
    """The ONE edit point must not silently contain a duplicate or be empty."""
    assert svc.CURATED_MODEL_IDS
    assert len(svc.CURATED_MODEL_IDS) == len(set(svc.CURATED_MODEL_IDS))
    assert all(isinstance(mid, str) and mid.strip() for mid in svc.CURATED_MODEL_IDS)
