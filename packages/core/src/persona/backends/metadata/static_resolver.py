"""Static metadata resolver — merges the per-provider tables (Spec 23 T3).

:class:`StaticModelMetadataResolver` implements
:class:`~persona.backends.model_metadata.ModelMetadataResolver` over the merged
per-provider tables. It is the **authoritative fallback** in the D-23-5 chain
(OpenRouter primary → static fallback → miss): it covers models absent from the
OpenRouter catalog and serves when the catalog is unavailable.

Lookup is an exact dict lookup on the canonical provider-prefixed id
(``"anthropic/claude-3.5-sonnet"``). A miss returns ``None`` — the chained
resolver moves on; if every resolver misses, the IntelligentRouter degrades to
rule-based selection (criterion 9).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.backends.metadata import anthropic, deepseek, google, groq, nvidia, openai

if TYPE_CHECKING:
    from persona.backends.model_metadata import ModelMetadata

__all__ = ["STATIC_MODEL_METADATA", "StaticModelMetadataResolver"]


def _merge() -> dict[str, ModelMetadata]:
    """Merge the per-provider tables into one id→metadata map.

    Keys are provider-prefixed and therefore disjoint across provider modules;
    a duplicate id across two modules is a maintenance bug and would surface as
    a last-writer-wins silent override — guarded against below.
    """
    merged: dict[str, ModelMetadata] = {}
    for table in (
        anthropic.MODELS,
        openai.MODELS,
        google.MODELS,
        deepseek.MODELS,
        groq.MODELS,
        nvidia.MODELS,
    ):
        for model_id, metadata in table.items():
            if model_id in merged:
                msg = f"duplicate static metadata id across provider tables: {model_id!r}"
                raise ValueError(msg)
            merged[model_id] = metadata
    return merged


STATIC_MODEL_METADATA: dict[str, ModelMetadata] = _merge()
"""The merged, read-at-import static metadata table (the one numbers home)."""


class StaticModelMetadataResolver:
    """Resolve model metadata from the static per-provider tables (D-23-5 fallback).

    Stateless and side-effect-free — safe to construct once and share. Implements
    the :class:`~persona.backends.model_metadata.ModelMetadataResolver` Protocol
    structurally.

    Args:
        table: Optional override of the merged table (tests inject a small map);
            defaults to :data:`STATIC_MODEL_METADATA`.
    """

    def __init__(self, table: dict[str, ModelMetadata] | None = None) -> None:
        self._table = table if table is not None else STATIC_MODEL_METADATA

    def resolve(self, model_id: str) -> ModelMetadata | None:
        """Return static metadata for ``model_id``, or ``None`` on a miss."""
        return self._table.get(model_id)
