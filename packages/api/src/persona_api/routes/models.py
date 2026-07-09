"""Model catalog route (Spec M1 T5) — curated shortlist + browse-all with prices.

``GET /v1/models`` serves the picker T7 consumes: the hand-kept curated shortlist
(default, ``?scope=recommended``) or the full browse-all catalog
(``?scope=all``), each entry priced in USD per 1M tokens. Authenticated —
consistent with the rest of the surface, same ``get_current_user`` idiom as
``routes/tools.py`` — but NOT RLS-scoped: this is platform-global catalog data,
not tenant data.

All filtering / pricing / fail-open logic lives in
:mod:`persona_api.services.model_catalog_service`; this module is a thin shape
translation (service dataclasses -> the pydantic wire contract T7 consumes
verbatim).
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict

from persona_api.auth import AuthenticatedUser, get_current_user
from persona_api.services import model_catalog_service

router = APIRouter(prefix="/v1", tags=["models"])

__all__ = ["ModelOut", "ModelsResponse", "router"]


class ModelOut(BaseModel):
    """One model in the ``GET /v1/models`` response (T7's web client consumes this verbatim).

    Attributes:
        id: The canonical OpenRouter model id.
        label: Human display name.
        provider: The id's provider prefix (e.g. ``"anthropic"``).
        input_price_per_1m: USD per 1,000,000 INPUT tokens.
        output_price_per_1m: USD per 1,000,000 OUTPUT tokens.
        context_length: Maximum context window, tokens.
        tools_supported: Whether the model advertises native tool calling.
        recommended: Whether this id is in the curated shortlist.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    label: str
    provider: str
    input_price_per_1m: float
    output_price_per_1m: float
    context_length: int
    tools_supported: bool
    recommended: bool


class ModelsResponse(BaseModel):
    """``GET /v1/models`` response envelope.

    Attributes:
        models: The filtered, ordered entries for the requested ``scope``.
        source: Always ``"openrouter"`` — the sole catalog source today.
        stale: ``True`` when the catalog could not be fetched (fail-open —
            ``models`` is then empty/partial, never a 500).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    models: list[ModelOut]
    source: Literal["openrouter"]
    stale: bool


@router.get("/models", response_model=ModelsResponse)
async def list_models(
    scope: Literal["recommended", "all"] = Query("recommended"),
    _user: AuthenticatedUser = Depends(get_current_user),
) -> ModelsResponse:
    """The curated shortlist (default) or the full browse-all catalog, with prices.

    Fail-open (spec_M1_design.md §4): a catalog fetch failure or missing
    OpenRouter configuration returns 200 with ``stale=True`` and an
    empty/partial ``models`` list — never a 500. A persona's turn still runs on
    the tier default regardless of this route's health.
    """
    result = model_catalog_service.list_models(scope=scope)
    return ModelsResponse(
        models=[
            ModelOut(
                id=m.id,
                label=m.label,
                provider=m.provider,
                input_price_per_1m=m.input_price_per_1m,
                output_price_per_1m=m.output_price_per_1m,
                context_length=m.context_length,
                tools_supported=m.tools_supported,
                recommended=m.recommended,
            )
            for m in result.models
        ],
        source=result.source,
        stale=result.stale,
    )
