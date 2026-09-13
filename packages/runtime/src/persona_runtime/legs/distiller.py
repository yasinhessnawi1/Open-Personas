"""The checkpoint distiller — the production CheckpointWriter (Spec A2, T12; D-A2-1).

Replaces the ``BasicCheckpointWriter`` stand-in in the live path. The stand-in appends every
leg's output and would eventually overflow the checkpoint budget
(:class:`~persona.errors.CheckpointTooLargeError`); the distiller **reflect-and-compacts** so a
many-leg task stays under the budget — the discipline D-A2-1 names ("maximise recall, then
precision; compact, don't truncate; keep pointers for detail").

This is the **deterministic, token-bounded** distiller (the live floor — guarantees no
overflow). A leg's new conclusion is appended; when the accumulating core approaches the
budget, the OLDEST conclusions are folded into a single ``[N earlier findings compacted]``
marker (their detail stays restorable via the workspace + the durable run records the
``event_log_cursor`` points at). A model-backed *semantic* distiller (better amnesia
resistance, gated by the T11 continuation eval) is the additive refinement that swaps in here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.skills import count_tokens
from persona.tasks import DEFAULT_CHECKPOINT_TOKEN_BUDGET, TaskCheckpoint

from persona_runtime.legs.ledger import (
    LEDGER_TOKEN_SHARE,
    fold_oldest,
    queries_from_run,
    sources_from_run,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from persona.tasks import Task

    from persona_runtime.agentic.run import Run

__all__ = ["CompactingCheckpointWriter"]


class CompactingCheckpointWriter:
    """A token-bounded :class:`CheckpointWriter` — appends, then reflect-and-compacts."""

    def __init__(self, *, token_budget: int = DEFAULT_CHECKPOINT_TOKEN_BUDGET) -> None:
        # Compact to a margin under the store's hard gate so the append always fits.
        self._target = int(token_budget * 0.8)
        # Spec W1 (D-W1-16): the query and source ledgers joined the accumulating core, so
        # they are inside the same gate. Each gets a small share and the conclusions get
        # what is left, which keeps the old behaviour when a task does no searching at all.
        self._ledger_target = int(token_budget * LEDGER_TOKEN_SHARE)

    async def write(
        self,
        *,
        task: Task,
        prior: TaskCheckpoint | None,
        run: Run,
        leg_id: str,
        seq: int,
        now: datetime,
    ) -> TaskCheckpoint:
        prior_conclusions = list(prior.progress_conclusions) if prior is not None else []
        if run.output:
            prior_conclusions.append(run.output)
        # Spec W1 (D-W1-16): what this leg asked and read, appended to what earlier legs did,
        # deduplicated so a query asked on three legs occupies one line.
        queries = fold_oldest(
            _merge(prior.queries_run if prior is not None else (), queries_from_run(run)),
            target_tokens=self._ledger_target,
            noun="queries",
        )
        sources = fold_oldest(
            _merge(prior.sources_seen if prior is not None else (), sources_from_run(run)),
            target_tokens=self._ledger_target,
            noun="sources",
        )
        # The ledgers are counted in the same core as the conclusions, so the conclusions
        # compact against what is actually left rather than against the whole budget.
        spent = count_tokens(" ".join((*queries, *sources)))
        conclusions = self._compact(prior_conclusions, target=self._target - spent)
        return TaskCheckpoint(
            task_id=task.id,
            leg_id=leg_id,
            checkpoint_seq=seq,
            progress_conclusions=tuple(conclusions),
            # R9-103: EMPTY, never the leg's output. ``next_step`` is defined as "the
            # single concrete action this leg's successor runs first", and
            # ``reconstruction`` recites it verbatim as ``NEXT STEP: …``. Assigning
            # ``run.output`` therefore handed the successor a finished ANSWER as its
            # INSTRUCTION, so it re-derived the same answer and wrote it back as the
            # next ``next_step`` — a closed loop. Observed in production: a recurring
            # task emitted byte-identical output every hour, its progress log filled
            # with copies of one paragraph, and it paid full price each fire to repeat
            # an answer it already had. The ``or prior.next_step`` fallback compounded
            # it, propagating a bad value forward indefinitely.
            #
            # This writer is DETERMINISTIC (the module docstring's "live floor"), so it
            # cannot GENERATE a next action — that is the model-backed semantic
            # distiller named as the additive refinement, which has not landed. Until
            # it does, empty is strictly better than an echo: ``reconstruction`` omits
            # the block entirely when it is falsy, so the successor plans afresh from
            # the contract plus ``progress_conclusions`` (which still carry every
            # finding). Empty loses continuity; the echo guaranteed repetition.
            next_step="",
            queries_run=queries,
            sources_seen=sources,
            open_questions=prior.open_questions if prior is not None else (),
            artifact_pointers=prior.artifact_pointers if prior is not None else (),
            event_log_cursor=run.id,  # the durable run record holds the compacted detail
            updated_at=now,
        )

    def _compact(self, conclusions: list[str], *, target: int) -> list[str]:
        """Fold the oldest conclusions into a marker until the core fits the target budget.

        Keeps the most recent conclusions verbatim (recency) and replaces the dropped prefix
        with one ``[N earlier findings compacted]`` line: bounded, restorable, never truncated
        mid-thought.
        """
        if count_tokens(" ".join(conclusions)) <= target:
            return conclusions
        compacted = 0
        kept = list(conclusions)
        while len(kept) > 1 and count_tokens(" ".join(kept)) > target:
            kept.pop(0)
            compacted += 1
        if compacted:
            return [f"[{compacted} earlier findings compacted, see run records]", *kept]
        return kept


def _merge(prior: Sequence[str], fresh: Sequence[str]) -> list[str]:
    """Earlier entries first, this leg's next, each entry once."""
    seen: dict[str, None] = {}
    for entry in (*prior, *fresh):
        seen.setdefault(entry, None)
    return list(seen)
