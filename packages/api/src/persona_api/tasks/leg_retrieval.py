"""What the persona already knows, fetched for the leg about to run (Spec W1, T12).

A leg's reconstruction has always had a RETRIEVAL slot and nothing ever filled it
(research V-3). So a recurring task asked the world every fire for things its own persona
had known for weeks, and the "live retrieval brings today's knowledge" half of the
reconstruction's design (D-A2-3) was a comment rather than a behaviour.

This fills it, through the recall path the chat loop already uses: episodic memory plus the
gated graph, scoped to the persona, queried with the CONTRACT GOAL. The goal, not the
reconstruction text: the goal is what the task is for, while the reconstruction is mostly
bookkeeping and would pull the retrieval toward whatever the last leg happened to say.

Three properties, all of them about not making a leg worse:

- **Off the loop.** The recall path is synchronous and does real work (embeddings, a
  cross-encoder). Run inline it would block the worker's event loop for as long as it takes.
- **Timed out.** Past the deadline the leg runs MEMORYLESS rather than late. The V13 posture:
  a leg that starts without its memory does the work; a leg that waits does nothing.
- **Never fatal.** Any failure returns nothing. Memory is an improvement to a leg, never a
  precondition for it, and a task that cannot run because recall is down is a worse product
  than one that runs without it.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Final

from persona.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from persona_runtime.unified_recall import UnifiedProjection

__all__ = ["DEFAULT_RETRIEVAL_TIMEOUT_S", "LegRetrieval", "RECALL_SNIPPET_CAP"]

_log = get_logger("tasks.leg_retrieval")

#: How long a leg waits for its memory before starting without it. Generous next to a chat
#: turn's budget (nobody is watching a cursor blink) and small next to a leg's wall-clock.
DEFAULT_RETRIEVAL_TIMEOUT_S: Final = 5.0

#: How many snippets reach the reconstruction. The block is context the leg reads before it
#: works, not the work itself; past a handful it crowds out the contract it sits under.
RECALL_SNIPPET_CAP: Final = 8


class LegRetrieval:
    """Fetches a leg's memory snippets off-loop, or gives up quietly (Spec W1, T12).

    Args:
        recall_for: Builds the persona's recall callable, or ``None`` when recall is not
            configured for this install (the K9 kill switch, no graph store, community
            without the stores). ``None`` means every leg runs memoryless, exactly as
            every leg did before this existed.
        timeout_s: How long to wait before the leg starts without its memory.
        snippet_cap: The most snippets a leg carries.
    """

    def __init__(
        self,
        *,
        recall_for: Callable[[str], Callable[[str], UnifiedProjection] | None],
        timeout_s: float = DEFAULT_RETRIEVAL_TIMEOUT_S,
        snippet_cap: int = RECALL_SNIPPET_CAP,
    ) -> None:
        self._recall_for = recall_for
        self._timeout_s = timeout_s
        self._cap = snippet_cap

    async def snippets(self, persona_id: str, goal: str) -> tuple[str, ...]:
        """The persona's own knowledge bearing on ``goal``, or ``()`` (never raises)."""
        if not goal.strip():
            return ()
        try:
            recall = self._recall_for(persona_id)
        except Exception as exc:  # noqa: BLE001 - memory is never a precondition for work
            _log.info("leg retrieval unavailable persona_id={pid}: {err}", pid=persona_id, err=exc)
            return ()
        if recall is None:
            return ()
        try:
            projection = await asyncio.wait_for(
                asyncio.to_thread(recall, goal), timeout=self._timeout_s
            )
        except TimeoutError:
            # The V13 posture, said plainly: the leg proceeds memoryless.
            _log.info(
                "leg retrieval timed out persona_id={pid}; running memoryless", pid=persona_id
            )
            return ()
        except Exception as exc:  # noqa: BLE001 - same reason as above
            _log.info("leg retrieval failed persona_id={pid}: {err}", pid=persona_id, err=exc)
            return ()
        return self._render(projection)

    def _render(self, projection: UnifiedProjection) -> tuple[str, ...]:
        """Flatten a projection into the plain lines the reconstruction renders.

        An abstaining gate (K9-D-9) yields nothing: the gate decided this leg has no
        business reading that memory, and a leg is not a reason to overrule it.
        """
        if projection.abstained:
            return ()
        lines: list[str] = []
        for item in projection.graph.items:
            lines.append(f"{item.concept_name}: {item.content}")
        lines.extend(chunk.text for chunk in projection.episodic)
        return tuple(_trimmed(lines)[: self._cap])


def _trimmed(lines: Sequence[str]) -> list[str]:
    """Non-empty, single-line, in order."""
    return [" ".join(line.split()) for line in lines if line and line.strip()]
