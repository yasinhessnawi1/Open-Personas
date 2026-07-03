"""Long-interaction windowing for synthesis (Spec K2, T8b; K2-D-5).

Bounds the extraction pass and protects the grounding rule. A very long
interaction is fed as:

- the compacted summary as **context** (explicitly marked *do not quote*) — it
  carries the already-synthesised prefix's gist so the extractor understands the
  tail, but it can NEVER be a candidate's grounding (a summary can compress
  inference into prose; only verbatim user text grounds a fact, K2-D-5);
- the **verbatim tail** — the messages past ``synthesised_up_to`` — as the
  grounding source (the only place evidence spans may be quoted from).

Returns ``None`` when there is nothing new past the marker — the idempotency
skip (a re-run over an already-synthesised interaction does no work).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from persona.extraction import ExtractionInput

if TYPE_CHECKING:
    from collections.abc import Sequence

    from persona.extraction import InteractionKind

__all__ = ["Window", "build_window"]

_CONTEXT_HEADER = "[Earlier context — for understanding only, do NOT quote as evidence]:"
_TRANSCRIPT_HEADER = "[Conversation — quote evidence ONLY from here]:"


@dataclass(frozen=True)
class Window:
    """A windowed extraction input plus the new high-water-mark to persist."""

    input: ExtractionInput
    high_water_mark: int


def build_window(
    *,
    messages: Sequence[tuple[str, str]],
    compacted_summary: str,
    synthesised_up_to: int,
    interaction_kind: InteractionKind,
    interaction_id: str,
    persona_id: str,
    channel: str = "chat",
) -> Window | None:
    """Window an interaction for synthesis, or ``None`` if nothing is new.

    Args:
        messages: The interaction's ``(role, content)`` turns, in order.
        compacted_summary: The already-synthesised prefix's summary (context only).
        synthesised_up_to: The high-water-mark — messages at indices below this are
            already synthesised and are not re-grounded.
        interaction_kind / interaction_id / persona_id: provenance material.
        channel: The originating surface (``chat`` / ``voice``, Spec V13) → the
            minted node's provenance channel. Defaults to ``chat``.
    """
    total = len(messages)
    if total <= synthesised_up_to:
        return None  # nothing new past the marker — the idempotency skip

    tail = messages[synthesised_up_to:]
    transcript = "\n".join(f"{role}: {content}" for role, content in tail)

    parts: list[str] = []
    if compacted_summary.strip():
        parts.append(f"{_CONTEXT_HEADER}\n{compacted_summary.strip()}")
    parts.append(f"{_TRANSCRIPT_HEADER}\n{transcript}")
    content = "\n\n".join(parts)

    return Window(
        input=ExtractionInput(
            interaction_kind=interaction_kind,
            interaction_id=interaction_id,
            persona_id=persona_id,
            content=content,
            channel=channel,
        ),
        high_water_mark=total,
    )
