"""The acceptance assessor — reads a leg, proposes which criteria it met (R9-164).

Acceptance criteria never moved off ``pending``. Nothing in the system could say a criterion
was met, so a week-long task re-read its whole checklist as unfinished on every leg and a
finished task reported nothing as done.

Judging "is this criterion met" is semantic: a deterministic rule cannot read "the report
compares at least three providers" against a leg's work. So a model reads the leg and
proposes. It is kept **separate from the checkpoint distiller on purpose**, even though the
distiller already reads the same leg and one call would be cheaper. They fail differently: a
bad distillation is a poor summary the next leg works around, while a bad acceptance claim is
the system telling the user their work is finished when it is not. Different blast radius,
different component, its own kill switch, and above all its own gate
(:func:`persona.tasks.acceptance.settle_criteria`), which lives in core where no prompt of
this module's can reach it.

Three properties hold it down:

- **It proposes; core decides.** Every claim goes through the gate, which refuses unknown
  ids, settled criteria, a leg that errored, and any claim whose evidence names nothing the
  leg actually produced or read.
- **It cannot touch the wording.** A claim is an id plus a status. The ids and statements are
  re-checked against the contract at the write (``Task.settle_criteria``), so there is no
  path from here to editing what "done" means.
- **Silence is the failure mode.** No backend, a timeout, a refusal, unparseable output: all
  of them return no claims, and the criteria stay exactly as they were. Nothing about a leg's
  real work depends on this running.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from persona.logging import get_logger
from persona.schema.conversation import ConversationMessage
from persona.tasks import AcceptanceStatus, CriterionClaim, LegEvidence

from persona_runtime.agentic.run import RunStatus
from persona_runtime.legs.ledger import artifacts_from_run, sources_from_run

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from persona.backends import ChatBackend
    from persona.tasks import AcceptanceCriterion, Contract

    from persona_runtime.agentic.run import Run

__all__ = [
    "ACCEPTANCE_PROMPT_VERSION",
    "DEFAULT_ASSESS_TIMEOUT_S",
    "AcceptanceAssessor",
    "evidence_from_run",
]

_log = get_logger("legs.acceptance")

#: Bump on any wording change to the prompt below, so a recorded result names what it judged.
ACCEPTANCE_PROMPT_VERSION: Final = "r9-acceptance-v1"

#: How long the assessment may take before the leg gives up on it. Shorter than the
#: distiller's: the distillation decides what the next leg knows and is worth waiting for,
#: while a missed criterion simply stays pending and the next leg can claim it.
DEFAULT_ASSESS_TIMEOUT_S: Final = 20.0

_MAX_STEPS_RENDERED: Final = 12
_MAX_STEP_CHARS: Final = 400
_MAX_TOKENS: Final = 500
_MAX_EVIDENCE_CHARS: Final = 300

_SYSTEM_PROMPT = """\
You are checking a long task's acceptance criteria against one leg of work that just \
finished. You are not the one who did the work and you are not here to be encouraging.

For each criterion you are shown, decide whether THIS leg settled it.

Rules:
- Claim "done" ONLY if the criterion is fully met. Partly met is not met. If the work is \
heading in the right direction but is not finished, say nothing about that criterion.
- Every "done" claim must quote the exact file path or the exact source URL from the FACTS \
list that shows it. A claim that cites nothing from that list is discarded, so do not guess \
or paraphrase a path.
- Claim "failed" only when the leg established that the criterion cannot be met as written.
- Most legs settle nothing. An empty list is the normal answer and is always safe.

Reply with ONLY a JSON object, no prose:
{"claims": [{"id": "...", "status": "done" | "failed", "evidence": "..."}]}
"""

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def evidence_from_run(run: Run) -> LegEvidence:
    """The durable facts about this leg that a claim is allowed to cite.

    This leg's own files and sources, never the task's accumulated ledgers: a leg that did
    nothing this time must not be able to settle a criterion on what an earlier leg read.
    """
    return LegEvidence(
        artifacts=tuple(pointer.ref for pointer in artifacts_from_run(run)),
        sources=sources_from_run(run),
        errored=run.status is RunStatus.ERROR,
    )


class AcceptanceAssessor:
    """Proposes which acceptance criteria a leg settled. Core decides whether it may.

    Args:
        backend_provider: Resolves the small-tier backend at assess time (the worker binds
            the owner scope per job). ``None`` is the honest "no model here" and yields no
            claims.
        timeout_s: How long the assessment may take before the leg moves on without it.
    """

    def __init__(
        self,
        *,
        backend_provider: Callable[[], ChatBackend | None],
        timeout_s: float = DEFAULT_ASSESS_TIMEOUT_S,
    ) -> None:
        self._backend_provider = backend_provider
        self._timeout_s = timeout_s

    async def assess(self, *, contract: Contract, run: Run) -> tuple[CriterionClaim, ...]:
        """Which criteria this leg claims to have settled (proposals, not decisions).

        Args:
            contract: The task's contract; only its unsettled criteria are put to the model.
            run: The finished leg.

        Returns:
            The claims to feed :func:`persona.tasks.acceptance.settle_criteria`. Empty
            whenever there is nothing to judge or anything at all went wrong.
        """
        open_criteria = [
            c for c in contract.acceptance_criteria if c.status is not AcceptanceStatus.DONE
        ]
        if not open_criteria:
            return ()
        evidence = evidence_from_run(run)
        if evidence.errored:
            # The gate would refuse every "done" claim from an errored leg anyway; not
            # paying for the call is the same answer, cheaper.
            return ()
        backend = self._backend_provider()
        if backend is None:
            return ()
        prompt = _render_prompt(criteria=open_criteria, run=run, evidence=evidence)
        asked_at = datetime.now(UTC)
        try:
            response = await asyncio.wait_for(
                backend.chat(
                    [
                        ConversationMessage(
                            role="system", content=_SYSTEM_PROMPT, created_at=asked_at
                        ),
                        ConversationMessage(role="user", content=prompt, created_at=asked_at),
                    ],
                    max_tokens=_MAX_TOKENS,
                ),
                timeout=self._timeout_s,
            )
        except TimeoutError:
            _log.info("acceptance assessment timed out; criteria unchanged")
            return ()
        except Exception as exc:  # noqa: BLE001 — a leg's work never depends on this call
            _log.info("acceptance assessment failed: {err}", err=exc)
            return ()
        return _parse_claims(response.content)


def _render_prompt(
    *, criteria: Sequence[AcceptanceCriterion], run: Run, evidence: LegEvidence
) -> str:
    """The criteria still open, the leg's work, and the facts a claim may cite."""
    lines = ["CRITERIA STILL OPEN:"]
    lines.extend(f"- {c.id} [{c.status.value}]: {c.statement}" for c in criteria)
    lines.append("\nFACTS FROM THIS LEG (the only things a 'done' claim may cite):")
    lines.extend(f"- file written: {path}" for path in evidence.artifacts)
    lines.extend(f"- source read: {url}" for url in evidence.sources)
    if not evidence.artifacts and not evidence.sources:
        lines.append("- (this leg wrote no file and read no source)")
    lines.append("\nTHIS LEG'S WORK:")
    lines.extend(_render_steps(run))
    if run.output:
        lines.append(f"\nTHIS LEG'S OUTPUT:\n{run.output[: _MAX_STEP_CHARS * 2]}")
    lines.append(f"\nTHIS LEG ENDED: {run.status}")
    return "\n".join(lines)


def _render_steps(run: Run) -> list[str]:
    rendered: list[str] = []
    for index, step in enumerate(run.steps[:_MAX_STEPS_RENDERED]):
        for call in step.tool_calls:
            rendered.append(f"- step {index}: called {call.name} {_short(str(call.args))}")
        for result in step.results:
            verb = "failed" if result.is_error else "returned"
            rendered.append(f"  {result.tool_name} {verb}: {_short(result.content)}")
        if step.content and not step.tool_calls:
            rendered.append(f"- step {index}: {_short(step.content)}")
    if len(run.steps) > _MAX_STEPS_RENDERED:
        rendered.append(f"- ({len(run.steps) - _MAX_STEPS_RENDERED} further steps not shown)")
    return rendered or ["- (the leg ran no steps)"]


def _short(text: str) -> str:
    return " ".join(text.split())[:_MAX_STEP_CHARS]


def _parse_claims(content: str) -> tuple[CriterionClaim, ...]:
    """The model's claims, or nothing at all.

    Anything malformed yields no claims rather than a best guess: a half-parsed status is
    how a criterion gets marked done by accident, which is the one outcome worth avoiding.
    """
    stripped = _FENCE_RE.sub("", content or "").strip()
    match = re.search(r"\{.*\}", stripped, re.DOTALL)
    if match is None:
        return ()
    try:
        parsed = json.loads(match.group(0))
    except (TypeError, ValueError):
        return ()
    if not isinstance(parsed, dict):
        return ()
    raw = parsed.get("claims")
    if not isinstance(raw, list):
        return ()
    claims: list[CriterionClaim] = []
    for item in raw:
        claim = _claim_from(item)
        if claim is not None:
            claims.append(claim)
    return tuple(claims)


def _claim_from(item: object) -> CriterionClaim | None:
    if not isinstance(item, dict):
        return None
    criterion_id = str(item.get("id", "")).strip()
    status = str(item.get("status", "")).strip().casefold()
    if not criterion_id or status not in {AcceptanceStatus.DONE, AcceptanceStatus.FAILED}:
        return None
    return CriterionClaim(
        criterion_id=criterion_id,
        status=AcceptanceStatus(status),
        evidence=str(item.get("evidence", "")).strip()[:_MAX_EVIDENCE_CHARS],
    )
