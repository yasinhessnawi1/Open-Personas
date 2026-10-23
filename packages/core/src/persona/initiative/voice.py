"""The initiative's user-facing voice guard (R9-256).

A candidate's ``why_now`` and ``next_step`` are read BY the user: the proposal says the
reason, then asks "Want me to <next step>?". The A5-R-1 texts talked about the user to
the user ("for the user to review", "the user expressed anxiety today"), because the
material the model reads is written about "the user". The prompts now ask for "you";
this is the cheap mechanical backstop behind them, pure and deterministic:

- a possessive ("the user's car") becomes "your car";
- "the user" as the OBJECT of a verb or preposition ("show it to the user", "for the
  user to review") becomes "you", but ONLY when what follows STARTS A CLAUSE OR A
  PHRASE: the end of the text, punctuation, or a word that can only open one ("to",
  "about", "whether", "before", "the", ...), never followed by a hyphen. After "let",
  "help" or "make" a bare verb opens the clause too ("let the user know");
- anything else is refused, because it cannot be rewritten safely: "The user has ...",
  "this user", a bare "user's", and every compound ("send the user manual", "the user
  ID", "the user-agreement", "the user to-do list", "the user in charge", "the user and
  group settings"). A refused text voids the candidate (the pipeline audits it,
  ``initiative.discard_voice``).

"and", "or" and "in" are never followers: after "the user" they so often continue a
compound ("the user and group settings", "the user in charge") that precision wins, and
"send it to the user and wait" voids rather than risks a garbled message.
"""

from __future__ import annotations

import re

__all__ = ["address_user"]

_APOSTROPHE = "['’]"
#: A possessive before a word ("the user's car"); a trailing one ("is the user's") is not
#: rewritten, since "is your" would read wrong, and so it is refused.
_POSSESSIVE_RE = re.compile(rf"\bthe\s+user{_APOSTROPHE}s\b(?=\s+\w)", re.IGNORECASE)
#: Words that can only START a clause or a phrase after an object "you".
_CLAUSE_OPENERS = (
    "to|about|whether|that|before|after|when|if|once|until|so|how|what|which|who|why|where|"
    "on|at|by|for|from|with|a|an|the|this|your"
)
#: Bare verbs that open the clause only after a causative ("let the user know").
_BARE_VERBS = (
    "know|review|decide|draft|approve|check|read|sign|see|confirm|choose|look|pick|compare"
)
_END = r"$|[.,;:!?)\]]"
_OBJECT_RE = re.compile(
    r"\b(for|to|with|from|by|about|remind|show|send|give|tell|ask|alert|notify)"
    rf"\s+the\s+user\b(?=\s*(?:{_END}|(?:{_CLAUSE_OPENERS})\b(?!-)))",
    re.IGNORECASE,
)
_CAUSATIVE_RE = re.compile(
    r"\b(let|help|make)"
    rf"\s+the\s+user\b(?=\s*(?:{_END}|(?:{_CLAUSE_OPENERS}|{_BARE_VERBS})\b(?!-)))",
    re.IGNORECASE,
)
_THIRD_PERSON_RE = re.compile(
    rf"\b(?:the|this|that)\s+user\b|\buser{_APOSTROPHE}s\b", re.IGNORECASE
)


def address_user(text: str) -> str | None:
    """``text`` addressed to the user as "you", or ``None`` if it cannot be made so."""
    out = _POSSESSIVE_RE.sub(lambda m: "Your" if m.group(0)[0].isupper() else "your", text)
    out = _OBJECT_RE.sub(lambda m: f"{m.group(1)} you", out)
    out = _CAUSATIVE_RE.sub(lambda m: f"{m.group(1)} you", out)
    return None if _THIRD_PERSON_RE.search(out) else out
