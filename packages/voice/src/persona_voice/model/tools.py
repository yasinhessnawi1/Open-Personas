"""Voice-tools design — the conservative-conversational v1 (spec V5 T7; D-V5-4/5).

A text turn can pause seconds for a tool; a voice turn cannot go silent that long
without feeling broken (R-V5-2). V5's honest v1 is **narrow + conversational**:

* :class:`VoiceToolPolicy` (D-V5-4) — partitions the persona's tools into
  ``VOICE_VIABLE`` (run live, preamble-masked, fast — e.g. web search with a
  spoken summary), ``DEFERRED`` (heavy — code execution / document generation:
  acknowledged in voice, done off the live path, delivered as an F5 artifact
  afterward), and ``TEXT_ONLY`` (not offered in voice at all).
* :class:`VoiceToolNarrator` (D-V5-5) — the preamble is a *first-class contract*,
  not the model's discretion: a rotated, action-phrased filler ("let me look that
  up") emitted the instant a live tool dispatches, varied across turns to avoid
  robotic repetition.
* :func:`run_tool_with_latency_bound` (D-V5-4-tool-latency-bound) — a preamble
  masks *expected* latency, but external tools have variable tails; a hard
  wall-clock bound guarantees an unbounded tool never strands the call (graceful
  overflow → the persona says it is taking a moment / falls back).
* :class:`DeferredArtifact` (D-V5-4-f5-artifact-shape) — the voice-side intent
  record for a deferred heavy tool: what the persona acknowledged it will prepare,
  to be produced off-path as an F5 artifact. **Forward-compatible, self-contained
  shape** — Spec 28 (rich-output-delivery, in flight) owns the *rendered* artifact
  (``PersistedArtifact`` / ``ToolResult.artifacts``, not yet on main); when it
  merges, the deferred path produces a Spec-28 artifact and this record references
  it. Surfaced for merge-back coordination (see closeout).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from collections.abc import Awaitable, Sequence

    from persona.backends.types import ToolSpec
    from persona.schema.tools import ToolResult

__all__ = [
    "DEFAULT_VOICE_TOOL_TIMEOUT_S",
    "BoundedToolOutcome",
    "DeferredArtifact",
    "VoiceToolDisposition",
    "VoiceToolNarrator",
    "VoiceToolPolicy",
    "run_tool_with_latency_bound",
]

#: Hard wall-clock ceiling for a live voice tool (s). Beyond this the call must
#: not sit in silence — the persona emits a graceful overflow line and falls back.
DEFAULT_VOICE_TOOL_TIMEOUT_S = 3.0

# V10-D-1 default partition — by MEASURED latency, not "visual vs text".
# INLINE-FAST (run live under the latency bound): web search/fetch + ``render_diagram``
# (sub-100ms — it persists mermaid/graphviz *source* and the browser renders the SVG,
# so it is no slower than a search). ASYNC_ARTIFACT (the render-when-ready lane):
# ``generate_image`` (5–20s remote call). DEFERRED (heavy / write, off the live path):
# ``code_execution`` + ``file_write``. Everything else → text-only — notably
# ``document_generation`` (no backend on main, V10-D-1) and any builtin not named here.
# NOTE: V5 deferred the dead string ``image_generation``; the real tool is
# ``generate_image`` (``persona.imagegen.tool`` ``@tool``), so image gen was silently
# never offered in voice — V10 makes it reachable. Constructor-overridable.
_DEFAULT_VOICE_VIABLE = frozenset({"web_search", "web_fetch", "render_diagram"})
_DEFAULT_ASYNC_ARTIFACT = frozenset({"generate_image"})
_DEFAULT_DEFERRED = frozenset({"code_execution", "file_write"})


class VoiceToolDisposition(StrEnum):
    """How a tool may be used in a live voice turn (D-V5-4 / V10-D-1)."""

    VOICE_VIABLE = "voice_viable"  # run live, preamble-masked, sub-budget
    ASYNC_ARTIFACT = "async_artifact"  # slow visual → produce off-turn, render-when-ready (V10)
    DEFERRED = "deferred"  # heavy → acknowledge in voice, produce off-path (F5)
    TEXT_ONLY = "text_only"  # not offered in voice


class VoiceToolPolicy:
    """Classifies the persona's tools for voice (the V10-D-1 latency partition).

    Args:
        voice_viable: Tool names runnable live (default: web search / fetch / diagram).
        async_artifact: Tool names that produce a slow visual artifact off the turn
            path and render-when-ready (default: ``generate_image``) — the V10 lane.
        deferred: Tool names acknowledged + done off-path (default: the heavy set).

    Any tool in none of the three sets is :attr:`VoiceToolDisposition.TEXT_ONLY`.
    """

    def __init__(
        self,
        *,
        voice_viable: frozenset[str] | None = None,
        async_artifact: frozenset[str] | None = None,
        deferred: frozenset[str] | None = None,
    ) -> None:
        self._viable = voice_viable if voice_viable is not None else _DEFAULT_VOICE_VIABLE
        self._async_artifact = (
            async_artifact if async_artifact is not None else _DEFAULT_ASYNC_ARTIFACT
        )
        self._deferred = deferred if deferred is not None else _DEFAULT_DEFERRED

    def classify(self, tool_name: str) -> VoiceToolDisposition:
        """Return the voice disposition of ``tool_name``."""
        if tool_name in self._viable:
            return VoiceToolDisposition.VOICE_VIABLE
        if tool_name in self._async_artifact:
            return VoiceToolDisposition.ASYNC_ARTIFACT
        if tool_name in self._deferred:
            return VoiceToolDisposition.DEFERRED
        return VoiceToolDisposition.TEXT_ONLY

    def offered_specs(self, specs: Sequence[ToolSpec]) -> list[ToolSpec]:
        """The specs offered to the model in voice — everything but text-only.

        Viable, async-artifact, AND deferred tools are all offered (the persona can
        reach the capability); only the disposition of a *call* differs — run live,
        produced render-when-ready, or acknowledged off-path (D-V5-4 / V10-D-1).
        Text-only tools are withheld from the voice surface entirely.
        """
        return [s for s in specs if self.classify(s.name) is not VoiceToolDisposition.TEXT_ONLY]


class VoiceToolNarrator:
    """The preamble-as-contract for voice tool use (D-V5-5).

    Args:
        preambles: The rotated action-phrased filler pool spoken while a live tool
            runs. Rotated by turn index so wording varies (avoids robotic repeats).
        deferral_line: What the persona says when acknowledging a deferred tool.
        overflow_line: What the persona says when a live tool overruns its bound.

    Raises:
        ValueError: ``preambles`` is empty.
    """

    def __init__(
        self,
        *,
        preambles: Sequence[str] = (
            "Let me look that up.",
            "One moment while I check that.",
            "Let me find that for you.",
            "Give me a second to pull that up.",
        ),
        deferral_line: str = "I'll prepare that and have it ready for you after our call.",
        overflow_line: str = "This is taking a moment. I'll follow up on that shortly.",
        async_artifact_line: str = (
            "Let me put that together. It'll appear on your screen in a moment."
        ),
    ) -> None:
        if not preambles:
            msg = "preambles must be non-empty"
            raise ValueError(msg)
        self._preambles = tuple(preambles)
        self._deferral = deferral_line
        self._overflow = overflow_line
        self._async_artifact = async_artifact_line

    def preamble(self, *, index: int) -> str:
        """The spoken filler for a live tool call, rotated by turn ``index``."""
        return self._preambles[index % len(self._preambles)]

    @property
    def deferral_line(self) -> str:
        """What the persona says when acknowledging a deferred heavy tool."""
        return self._deferral

    @property
    def overflow_line(self) -> str:
        """What the persona says when a live tool exceeds its latency bound."""
        return self._overflow

    @property
    def async_artifact_line(self) -> str:
        """The inline ack when a slow visual artifact is handed to the off-turn lane.

        Spoken at request time (V10-D-3) so the screen-update-when-ready is never a
        surprise; the persona's "it's on screen" confirmation comes later, at the
        next idle floor (the orchestrator's floor-gated narration).
        """
        return self._async_artifact


@dataclass(frozen=True)
class BoundedToolOutcome:
    """The result of a latency-bounded tool dispatch (D-V5-4-tool-latency-bound)."""

    result: ToolResult | None
    timed_out: bool


async def run_tool_with_latency_bound(
    dispatch: Awaitable[ToolResult], *, timeout_s: float = DEFAULT_VOICE_TOOL_TIMEOUT_S
) -> BoundedToolOutcome:
    """Run a tool dispatch under a hard wall-clock bound (never strand the call).

    On timeout the dispatch is cancelled and ``timed_out=True`` is returned so the
    caller can speak the graceful overflow line and fall back — an unbounded tool
    must never leave the live voice turn in silence (D-V5-4-tool-latency-bound).
    """
    try:
        result = await asyncio.wait_for(dispatch, timeout_s)
    except TimeoutError:
        return BoundedToolOutcome(result=None, timed_out=True)
    return BoundedToolOutcome(result=result, timed_out=False)


class DeferredArtifact(BaseModel):
    """A heavy tool the persona acknowledged in voice, to be produced off-path (F5).

    The voice-side INTENT record (D-V5-4-f5-artifact-shape): the persona said it
    will prepare something heavy; the actual rendered artifact is produced off the
    live path and delivered as a Spec 28 F5 artifact. This shape is self-contained
    and forward-compatible — when Spec 28's ``PersistedArtifact`` /
    ``ToolResult.artifacts`` land on main, the produced artifact's id/path is the
    fulfilment of this intent (coordination flagged at close-out).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    spoken_acknowledgement: str
