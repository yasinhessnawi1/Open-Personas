"""The durable wellbeing disclosure fact, as the write paths see it (R9-253).

Initiative's wellbeing gate must remember that an interaction carried a disclosure even after
the user forgets the tagged node, so the untagged nodes learned beside it stay out of the
unprompted scan. The two writers that tag (synthesis and ``record_user_fact``) record the fact
through this port BEFORE they write the node; the api implements it over its
``wellbeing_disclosures`` table (``persona_api.initiative.disclosures.PgDisclosureStore``).

What counts as a disclosure here: a candidate carrying a wellbeing category, or one rejected
for self-harm means (that disclosure leaves no node behind at all; D-ORCH-11-18-m). The fact
holds ids only, never the text or the category.
"""

from __future__ import annotations

from typing import Protocol

from persona_runtime.extraction.means_guard import contains_self_harm_means

__all__ = ["DisclosureRecorder", "is_disclosure"]


class DisclosureRecorder(Protocol):
    """Records that an interaction carried a wellbeing disclosure. Idempotent."""

    def record(self, owner_id: str, persona_id: str, interaction_id: str) -> bool:
        """Hold the fact; ``False`` when the persona no longer exists (write nothing then).

        Any other failure raises, so the writer fails closed.
        """
        ...


def is_disclosure(content: str, category: str | None) -> bool:
    """Whether a write is a wellbeing disclosure: tagged, or carrying self-harm means."""
    return category is not None or contains_self_harm_means(content, category=category)
