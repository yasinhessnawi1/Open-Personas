"""Question authoring — turn an ambiguity signal into a 3+1 question (D-21-14).

D-21-14 locks option generation as a small-tier **model call** seeded by the
detector signal, with a **mandatory deterministic template fallback** for when
the call fails/times out or the persona's tier has no structured-output model.
This module ships the fallback (and the injectable seam):

- :class:`QuestionAuthor` — the async port the loops call to author a
  :class:`~persona_runtime.questions.ProactiveQuestion` from a signal. A
  model-backed implementation is injected when available; until then the loops
  use the template author.
- :class:`TemplateQuestionAuthor` — the deterministic fallback. Class A and D
  questions are fully templatable (R-21-2); class B/C templates are generic and
  lean on the free-form slot (the model author is the quality path for those).

Templates are localised EN / Norwegian Bokmål off the persona language.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from persona.autonomy import AmbiguityClass

from persona_runtime.questions import ProactiveQuestion, QuestionOption

if TYPE_CHECKING:
    from persona_runtime.ambiguity import AmbiguitySignal

__all__ = ["QuestionAuthor", "TemplateQuestionAuthor"]


@runtime_checkable
class QuestionAuthor(Protocol):
    """Authors a :class:`ProactiveQuestion` from a detected ambiguity signal.

    Async so a model-backed author (the D-21-14 primary) fits the same port as
    the deterministic template fallback.
    """

    async def author(
        self, message: str, signal: AmbiguitySignal, *, language: str
    ) -> ProactiveQuestion:
        """Return the 3+1 question to ask for ``signal`` on ``message``."""
        ...

    async def default(self, question: str, *, language: str) -> ProactiveQuestion:
        """Wrap a model-authored ``question`` with generic 3+1 options.

        Used by the agentic loop, where the model writes the question text
        (``[ASK_USER]``) and the author only supplies the option scaffold so the
        web renders the structured 3+1 UI (D-21-9). The free-form slot lets the
        user answer beyond the generic options.
        """
        ...


def _is_norwegian(language: str) -> bool:
    return language.lower() in {"no", "nb", "nn", "nob"}


def _opts(*labels: str) -> tuple[QuestionOption, ...]:
    return tuple(QuestionOption(label=label) for label in labels)


class TemplateQuestionAuthor:
    """Deterministic template author — the D-21-14 mandatory fallback.

    Pure and synchronous-bodied (the ``async`` is the port's, not a real await),
    so it never adds latency and always succeeds. Keys templates off the
    signal's class + ``missing_element`` + language.
    """

    async def author(
        self, message: str, signal: AmbiguitySignal, *, language: str
    ) -> ProactiveQuestion:
        """Build a templated 3+1 question (see module docstring)."""
        _ = message  # the template path is content-free; the model author uses it
        norwegian = _is_norwegian(language)
        if signal.signal_class is AmbiguityClass.SAFETY_CRITICAL_GAP:
            return self._safety(signal, norwegian=norwegian)
        if signal.signal_class is AmbiguityClass.MISSING_PARAMETER:
            return self._missing_parameter(signal, norwegian=norwegian)
        return self._vague(norwegian=norwegian)

    async def default(self, question: str, *, language: str) -> ProactiveQuestion:
        """Wrap a model-authored question with generic yes/no/explain options."""
        if _is_norwegian(language):
            return ProactiveQuestion(
                question=question,
                options=_opts("Ja", "Nei", "La meg forklare"),
            )
        return ProactiveQuestion(
            question=question,
            options=_opts("Yes", "No", "Let me explain"),
        )

    def _safety(self, signal: AmbiguitySignal, *, norwegian: bool) -> ProactiveQuestion:
        if norwegian:
            return ProactiveQuestion(
                question=f"Dette kan påvirke {signal.missing_element} permanent. "
                "Hvordan vil du at jeg skal fortsette?",
                options=_opts("Ja, fortsett", "Nei, avbryt", "Vis meg hva som påvirkes først"),
            )
        return ProactiveQuestion(
            question=f"This could permanently affect {signal.missing_element}. "
            "How should I proceed?",
            options=_opts("Yes, go ahead", "No, cancel this", "Show me what's affected first"),
        )

    def _missing_parameter(self, signal: AmbiguitySignal, *, norwegian: bool) -> ProactiveQuestion:
        element = signal.missing_element
        if norwegian:
            return ProactiveQuestion(
                question=f"Jeg trenger litt mer for å gå videre. Hva er {self._no_word(element)}?",
                options=_opts("Jeg oppgir det", "Bruk ditt beste skjønn", "Vent litt foreløpig"),
            )
        return ProactiveQuestion(
            question=f"I need one more detail to proceed. What's the {element}?",
            options=_opts("I'll specify it", "Use your best judgment", "Hold off for now"),
        )

    def _vague(self, *, norwegian: bool) -> ProactiveQuestion:
        if norwegian:
            return ProactiveQuestion(
                question="Før jeg starter, kan du si litt om hva fokuset skal være?",
                options=_opts("Jeg beskriver det", "Bruk ditt beste skjønn", "La meg avgrense det"),
            )
        return ProactiveQuestion(
            question="Before I start, can you tell me what the focus should be?",
            options=_opts("I'll describe it", "Use your best judgment", "Let me narrow it down"),
        )

    @staticmethod
    def _no_word(element: str) -> str:
        """Map an English missing-element token to a Norwegian noun for the template."""
        return {
            "recipient": "mottakeren",
            "time": "tidspunktet",
            "amount": "beløpet",
            "subject": "emnet",
            "target": "målet",
        }.get(element, "detaljen")
