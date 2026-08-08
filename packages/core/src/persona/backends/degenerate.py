"""Detect a completion that has collapsed into a repetition loop (R9-090).

A persona was asked for a task's status and answered with roughly 4000 tokens of
token salad, delivered to the user AS THE PERSONA'S VOICE. It was served by
``nvidia/nemotron-3-super-120b-a12b:free``, sitting last in both the free frontier
and free mid chains, so it served precisely when the first two rate-limited, which
under load is often. ``completion_tokens`` was exactly the 4096 output cap: the
model ran until truncation rather than finishing. Every neighbouring turn on a
different model returned 92 to 3386 tokens of normal prose, and the same
conversation compacted three minutes earlier without incident, so the model was
the cause and not the context.

That is worse than an error. An error says the product is busy; this says the
persona is broken, costs a full-cap generation, and is indistinguishable to the
reader from us shipping nonsense.

**What this module claims, and what it does not.** It detects the *repetition*
collapse: text whose distinct-word ratio is far below anything human-readable
prose produces. Measured against real assistant replies from production, healthy
prose sits at 0.70 to 0.81 distinct words per word; the threshold here is a
fraction of that, so a legitimate reply cannot trip it. It does NOT attempt to
detect high-entropy gibberish that never repeats, because separating that from
code, non-English text, base64 or structured output needs a real classifier, and
one that misfires deletes good replies. A guard that is certain about a narrow
case beats a guess about a wide one.

Length matters as much as ratio: short text is legitimately repetitive ("yes,
yes, yes"), so the check applies only above a floor where the ratio is meaningful.
"""

from __future__ import annotations

__all__ = [
    "DEGENERATE_MIN_WORDS",
    "DEGENERATE_RATIO",
    "is_degenerate_repetition",
]

#: Distinct words per word, below which the text is a repetition loop rather
#: than a reply. Production assistant messages measure 0.70 to 0.81; a model
#: cycling a phrase lands near zero. The gap is wide enough that this constant
#: does not need tuning, and deliberately sits nowhere near real prose so the
#: guard errs toward letting a bad reply through rather than eating a good one.
DEGENERATE_RATIO = 0.15

#: Below this many words the ratio says nothing: a short answer is allowed to be
#: repetitive, and a two-word reply always scores badly. The observed failure ran
#: to the full output cap, so it clears this floor by an order of magnitude.
DEGENERATE_MIN_WORDS = 400


def is_degenerate_repetition(text: str) -> bool:
    """Whether ``text`` has collapsed into a repetition loop.

    Args:
        text: The accumulated completion text.

    Returns:
        ``True`` only when the text is long enough for the measure to mean
        something AND its distinct-word ratio is far below readable prose.
    """
    words = text.split()
    if len(words) < DEGENERATE_MIN_WORDS:
        return False
    return len(set(words)) / len(words) < DEGENERATE_RATIO
