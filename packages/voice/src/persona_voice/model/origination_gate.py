"""The voice task-origination gate — the A4 grammar, spoken (Spec A9, T2; A9-D-2/D-3/D-4).

One origination grammar, two channels. This gate composes A4's **verbatim** pieces — the
:class:`~persona_runtime.task_origination.StandingIntentRecognizer` (cue net + model judge), the
:func:`~persona_runtime.task_origination.is_affirmative_confirmation` confirm test, the
:class:`~persona_runtime.task_origination.AmendmentInterpreter`, and the ``render_echo`` VOICE mode
— into the spoken contract flow. It is **pure decision logic**: it owns no I/O, no store, no event
loop, and no graph; it takes a user transcript and returns what the persona should say (or falls
through to ordinary chat), and — on a clean spoken confirm — the draft to originate. The reply
producer (T3) wires it into the post-STT lane behind the safety bypass; the create side effect
(T4/T5) rides a durable job, never this gate.

The confabulation guarantee holds structurally: the gate **never creates anything**. A ``STANDING``
recognition only *proposes* a spoken echo; creation still requires an explicit spoken confirmation,
and even then the gate only *returns the confirmed draft* — the enqueue is the caller's.

Voice-specific window semantics (A9-D-3):

* **Next-turn-only.** An armed proposal is confirmable only on the immediately-following user turn;
  any non-confirm, non-amendment turn lapses it (the flow falls through to ordinary chat — chat's
  exact abandon behaviour) so a stale "yes" three turns later can never create a task.
* **Barge-invalidated.** A spoken echo the user interrupted (truncated) never arms a confirmable
  proposal — a later "yes" would confirm a contract they never heard in full. The caller reports the
  truncation via :meth:`note_spoken_turn_committed`.
* **Timeout.** A short inactivity window bounds a proposal that is never answered.

Amendment depth (A9-D-4): one spoken amendment round, then the persona redirects the fine-tuning to
chat/A6 (voice is a poor medium for contract lawyering).

Fail-soft (criterion 7): the recognizer already degrades a judge failure to *ask-once*; this gate
adds nothing that can raise on the common path, and the caller wraps the whole gate so any
error/keyless/over-budget leaves today's clean call.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from persona.approvals.records import Materiality
from persona.logging import get_logger
from persona_runtime.task_origination import (
    Clause,
    ContractDraft,
    EchoMode,
    RecognitionKind,
    canonicalize_draft,
    changed_clauses,
    classify_amendment_materiality,
    detect_reschedule_cue,
    detect_steering_cue,
    is_affirmative_confirmation,
    render_clause,
    render_echo,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from persona_runtime.task_origination import (
        AmendmentInterpreter,
        StandingIntentRecognizer,
    )

    from persona_voice.turn_taking.heard_words import BargedReply

__all__ = [
    "DEFAULT_WINDOW_TIMEOUT_S",
    "MAX_VOICE_AMENDMENT_ROUNDS",
    "DelegatedTurnIntent",
    "GateCommitListener",
    "VoiceOriginationDecision",
    "VoiceOriginationGate",
]

_logger = get_logger("voice.origination_gate")

#: How long an armed proposal stays confirmable without an answer (A9-D-3 timeout). The
#: next-turn-only rule already bounds it to the immediately-following turn; this guards a long
#: silence before that turn arrives (a stale "yes" after a topic drift must not create a task).
DEFAULT_WINDOW_TIMEOUT_S: float = 90.0

#: The spoken amendment rounds a proposal tolerates before redirecting to chat (A9-D-4). Voice is a
#: poor medium for clause-by-clause contract editing, so one round covers the common tweak.
MAX_VOICE_AMENDMENT_ROUNDS: int = 1

# The deterministic spoken lines (short, warm — the V11 voice register; no markdown/URLs/lists).
# On confirm, voice says it is PREPARING in the background — NOT "done" (the redirect: execution is
# the chat pipeline's, off this loop; a "done" here would be a confabulated success — A9-D-6/D-7).
# The grounded "what was actually done" summary comes later, on the hand-back (T6).
_DELEGATING_TEXT = (
    "Got it — I'm setting that up in the background. I'll let you know when it's ready."
)
_REDIRECT_TO_CHAT_TEXT = (
    "Let's finish the details in chat — I've noted it, and you can fine-tune it there."
)
#: The spoken ack for a delegated STEERING/reschedule ask (T7). No voice-side echo/confirm: the
#: frontier applies pause/resume immediately, and a confirm-needed action (cancel / reschedule)
#: comes back via the honest-incomplete hand-back. The grounded result is spoken later (poller, T6).
_STEERING_ACK = "On it — let me take care of that in the background."


@dataclass(frozen=True)
class DelegatedTurnIntent:
    """The confirmed ask to delegate to the chat pipeline (Spec A9, A9-D-5/D-7).

    The gate produces this on a clean spoken confirm; the reply producer fills the conversation +
    persona from the session context and hands it to the delegation listener (T4), which enqueues
    the durable ``delegated_turn`` job. Carries the **verbatim spoken ask** — never the mid-model's
    parsed draft — so the frontier chat pipeline re-parses authoritatively (voice never
    parses-for-execution). ``provenance`` marks the originating surface for the audit row.
    """

    conversation_id: str
    verbatim_ask: str
    persona_id: str
    provenance: str = "voice"


@dataclass(frozen=True)
class VoiceOriginationDecision:
    """What the gate concluded for one user turn (Spec A9, T2).

    ``spoken`` is the persona's line for this turn, or ``None`` to fall through to ordinary
    generation (the gate did not own the turn). ``confirmed_draft`` + ``verbatim_ask`` are set
    **only** on a clean spoken confirmation — the draft grounds the summary/echo, the verbatim ask
    is what the caller **delegates** to the chat pipeline (the gate never creates or delegates
    itself). ``owns_turn`` is ``True`` whenever the gate produced a spoken line (echo / ask /
    confirm / amend / redirect), so the caller skips generation.
    """

    spoken: str | None
    confirmed_draft: ContractDraft | None = None
    verbatim_ask: str | None = None

    @property
    def owns_turn(self) -> bool:
        """Whether the gate owns this turn (produced a spoken line ⇒ skip generation)."""
        return self.spoken is not None

    @classmethod
    def ordinary(cls) -> VoiceOriginationDecision:
        """Fall through — the gate does not own this turn."""
        return cls(spoken=None)


@dataclass
class _ArmedProposal:
    """An echoed, confirmable proposal awaiting the immediately-following turn (A9-D-3)."""

    draft: ContractDraft
    verbatim_ask: str
    armed_at: datetime
    amendment_rounds: int = 0


@dataclass
class _TentativeProposal:
    """A proposal whose echo was spoken this turn, awaiting commit (arm iff not barged)."""

    draft: ContractDraft
    verbatim_ask: str
    amendment_rounds: int = 0


class VoiceOriginationGate:
    """The spoken contract flow — A4's grammar, voice mode, pure decision logic (Spec A9).

    Construct once per voice session. The gate is single-turn-sequential (a call is one
    conversation at a time), so it holds the pending-proposal state directly; the reply producer
    calls :meth:`on_user_turn` per turn and :meth:`note_spoken_turn_committed` after the spoken
    turn commits (to arm-or-invalidate on barge-in).

    Args:
        recognizer: A4's standing-intent recognizer (cue net + model judge), reused verbatim.
        amendment_interpreter: A4's amendment interpreter; ``None`` disables amendment (a
            non-confirm reply then lapses the proposal — still safe, just less ergonomic).
        language: The persona's default language (drives the recognizer's ask-once localisation).
        window_timeout_s: The inactivity window an armed proposal survives (A9-D-3).
        max_amendment_rounds: Spoken amendment rounds before redirecting to chat (A9-D-4).
        clock: UTC-now provider (injected for deterministic tests).
    """

    def __init__(
        self,
        *,
        recognizer: StandingIntentRecognizer,
        amendment_interpreter: AmendmentInterpreter | None,
        language: str,
        window_timeout_s: float = DEFAULT_WINDOW_TIMEOUT_S,
        max_amendment_rounds: int = MAX_VOICE_AMENDMENT_ROUNDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._recognizer = recognizer
        self._amendment = amendment_interpreter
        self._language = language
        self._window_timeout_s = window_timeout_s
        self._max_rounds = max_amendment_rounds
        self._clock = clock or (lambda: datetime.now(UTC))
        self._armed: _ArmedProposal | None = None
        self._tentative: _TentativeProposal | None = None

    async def on_user_turn(self, transcript: str) -> VoiceOriginationDecision:
        """Decide the gate's action for one user turn (Spec A9, T2).

        With an armed proposal (and within the window): a clean confirmation returns the draft to
        originate; a first amendment re-echoes and re-arms; a second amendment redirects to chat;
        any other reply lapses the proposal and falls through. With no armed proposal: the
        recognizer runs (behind its own cheap cue gate) — ``STANDING`` echoes a spoken contract,
        ``ASK_ONCE`` asks the one clarifying question, ``ORDINARY`` falls through. Conservative by
        construction: an unclear reply is never a created task (criterion 4).
        """
        # A spoken turn is about to be produced; any previously-tentative echo that was never
        # committed (should not happen — commit is reported per turn) is discarded here defensively.
        self._tentative = None
        armed = self._live_proposal()
        if armed is not None:
            return await self._on_pending_turn(transcript, armed)
        # A9-T7: a spoken STEERING/reschedule ask ("pause that", "cancel it", "move it to 9") rides
        # the SAME delegation crossing — cheap lexical cues gate it; the verbatim ask goes to
        # the frontier (pause/resume applied immediately; cancel/reschedule come back as the
        # honest-incomplete hand-back). Checked before standing recognition (steering is about an
        # EXISTING task, not a new one — the chat loop's ordering).
        if detect_steering_cue(transcript) or detect_reschedule_cue(transcript):
            return VoiceOriginationDecision(spoken=_STEERING_ACK, verbatim_ask=transcript)
        return await self._on_fresh_turn(transcript)

    def note_spoken_turn_committed(self, *, truncated: bool) -> None:
        """Arm (or invalidate) the proposal whose echo was just spoken (A9-D-3 barge rule).

        Called by the caller after the gate-owned spoken turn commits. A truncated (barged) echo
        does NOT arm a confirmable proposal — the user never heard the full contract, so a later
        "yes" cannot confirm it. An un-truncated echo arms the proposal for the immediately-next
        turn. A turn that armed nothing (a confirm/redirect/ordinary turn) is a no-op.
        """
        tentative = self._tentative
        self._tentative = None
        if tentative is None:
            return
        if truncated:
            _logger.info("voice echo barged before completion; proposal not armed (A9-D-3)")
            self._armed = None
            return
        self._armed = _ArmedProposal(
            draft=tentative.draft,
            verbatim_ask=tentative.verbatim_ask,
            armed_at=self._clock(),
            amendment_rounds=tentative.amendment_rounds,
        )

    # ----- internals ---------------------------------------------------

    def _live_proposal(self) -> _ArmedProposal | None:
        """The armed proposal if still within its window, else ``None`` (and cleared)."""
        armed = self._armed
        if armed is None:
            return None
        elapsed = (self._clock() - armed.armed_at).total_seconds()
        if elapsed > self._window_timeout_s:
            _logger.info("voice contract proposal window lapsed ({s:.0f}s)", s=elapsed)
            self._armed = None
            return None
        return armed

    async def _on_pending_turn(
        self, transcript: str, armed: _ArmedProposal
    ) -> VoiceOriginationDecision:
        """Handle a turn while a proposal is armed: confirm / amend / redirect / lapse."""
        if is_affirmative_confirmation(transcript):
            # The one explicit spoken confirmation → hand the VERBATIM ask to the caller to
            # delegate (A9-D-5/D-7). The gate creates/delegates nothing; it clears pending, speaks
            # the "preparing in the background" line, and returns the confirmed draft (grounding)
            # + the verbatim ask (what the chat pipeline re-parses on the frontier tier).
            self._armed = None
            return VoiceOriginationDecision(
                spoken=_DELEGATING_TEXT,
                confirmed_draft=armed.draft,
                verbatim_ask=armed.verbatim_ask,
            )

        amended = (
            await self._amendment.interpret(transcript, armed.draft)
            if self._amendment is not None
            else None
        )
        if amended is not None:
            if armed.amendment_rounds >= self._max_rounds:
                # A9-D-4: one spoken round only — redirect the fine-tuning to chat/A6.
                self._armed = None
                return VoiceOriginationDecision(spoken=_REDIRECT_TO_CHAT_TEXT)
            amended = canonicalize_draft(amended)
            reecho = self._render_amendment_voice(armed.draft, amended)
            # The delegated ask stays VERBATIM: append the amendment utterance to the original
            # spoken ask, so the frontier re-parses the full spoken intent (never the mid draft).
            self._tentative = _TentativeProposal(
                draft=amended,
                verbatim_ask=f"{armed.verbatim_ask} {transcript}".strip(),
                amendment_rounds=armed.amendment_rounds + 1,
            )
            self._armed = None  # re-armed on commit (barge rule applies to the re-echo too)
            return VoiceOriginationDecision(spoken=reecho)

        # Neither a clean confirmation nor an amendment → the proposal lapses; fall through to
        # ordinary chat (chat's exact abandon behaviour — a stale proposal never lingers).
        self._armed = None
        return VoiceOriginationDecision.ordinary()

    async def _on_fresh_turn(self, transcript: str) -> VoiceOriginationDecision:
        """Handle a turn with no armed proposal: recognize standing intent (cue gate first)."""
        outcome = await self._recognizer.recognize(transcript, language=self._language)
        if outcome.kind is RecognitionKind.STANDING and outcome.draft is not None:
            echo = render_echo(outcome.draft, EchoMode.VOICE)
            self._tentative = _TentativeProposal(draft=outcome.draft, verbatim_ask=transcript)
            return VoiceOriginationDecision(spoken=echo)
        if outcome.kind is RecognitionKind.ASK_ONCE and outcome.question is not None:
            # The one clarifying question ("on a schedule, or just this once?") — spoken; no
            # proposal armed (the answer re-enters recognition next turn), nothing to confirm here.
            return VoiceOriginationDecision(spoken=outcome.question.question)
        return VoiceOriginationDecision.ordinary()

    def _render_amendment_voice(self, before: ContractDraft, after: ContractDraft) -> str:
        """Re-echo an amendment for the ear (A9-D-2/D-4), mirroring the chat ``_render_amendment``.

        A material change re-echoes the whole contract for a fresh confirm; a tuning change
        re-echoes just the changed clause(s). Both keep the proposal pending (the user must still
        confirm). Voice-mode rendering: sentence-shaped, ending in the spoken confirm ask.
        """
        if classify_amendment_materiality(before, after) is Materiality.MATERIAL:
            updated = render_echo(after, EchoMode.VOICE)
            return f"That's a bigger change. Here's the updated plan. {updated}"
        clauses = changed_clauses(before, after) or (Clause.GOAL,)
        lines = " ".join(render_clause(after, clause, EchoMode.VOICE) for clause in clauses)
        return f"Done. {lines} Shall I go ahead?"


class GateCommitListener:
    """Feeds each turn's commit (heard reply + truncation) to the gate's barge rule (A9-D-3).

    Implements V4's ``TurnTranscriptListener`` structurally (``on_reply_committed``), so it composes
    into the loop's single transcript-listener slot alongside the V5 memory recorder via
    :class:`~persona_voice.turn_taking.bridge.CompositeTurnTranscriptListener`. It carries only the
    ``truncated`` flag to :meth:`VoiceOriginationGate.note_spoken_turn_committed`: a barged echo
    does not arm a confirmable proposal (the user never heard the full contract). MUST NOT raise
    (the loop runs commits in its ``finally``) — ``note_spoken_turn_committed`` is pure and total.
    """

    def __init__(self, gate: VoiceOriginationGate) -> None:
        self._gate = gate

    async def on_reply_committed(self, reply: BargedReply) -> None:
        """Arm-or-invalidate the pending proposal off this turn's truncation (A9-D-3)."""
        self._gate.note_spoken_turn_committed(truncated=reply.truncated)
