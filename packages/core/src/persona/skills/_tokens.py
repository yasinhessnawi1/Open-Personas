"""Token counting for the skills layer (T02, D-04-2).

Wraps ``tiktoken.get_encoding("cl100k_base")`` once at module import. The
encoder is provider-independent — the 2000-token budget enforced in
``persona.skills.injector`` is a budget, not a contract with any specific
model. Slight over/under-count vs the deployed model is fine.

``tiktoken`` is a core dep (D-01-11, declared in
``packages/core/pyproject.toml``). The import is hard at module load — there
is no ``len(text) // 4`` heuristic fallback (D-04-2). If ``tiktoken`` ever
fails to provide ``cl100k_base``, that is a packaging bug and we want to
crash at startup, not silently degrade the budget enforcement.

The encoder is cached at tiktoken's C layer (``get_encoding`` returns the
same object across calls). Stateless under concurrent access — safe to
share across threads / asyncio tasks without locking.

Text is encoded with ``disallowed_special=()``: a document or a message that
contains a special-token string such as ``<|endoftext|>`` is counted as the
ordinary text it is, instead of raising. For text without such strings the
count is the same as ``tiktoken``'s default ``encode``.

Long text is encoded in segments of at most :data:`SEGMENT_CHARS` characters.
tiktoken's pre-tokeniser fails on a whitespace run of about a million
characters ("Max stack size exceeded for backtracking"; through
``encode_ordinary`` it panics), so no single call may see that much. Segments
end where :func:`safe_cut` proves a cut cannot change the count, so the total
is exactly that of one whole encode, unless a segment's whole window has no
such point. That happens only in a window of :data:`SEGMENT_CHARS` characters
holding no space after a non-space and no line break before a non-space: one
unbroken line with no spaces (a run of one character, unspaced CJK, a base64
or minified blob) or nothing but whitespace. There the segment is cut hard, and
the count may differ from a whole encode by a few tokens at each hard cut.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import tiktoken

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = ["SEGMENT_CHARS", "count_tokens", "count_tokens_capped", "encode", "safe_cut"]

# Loaded once at module import. Cached at the C layer; a module-level
# singleton is idiomatic. Re-binding via ``_ENCODER = ...`` elsewhere is
# unsupported.
_ENCODER = tiktoken.get_encoding("cl100k_base")

#: The most characters handed to the encoder in one call. A whitespace run must
#: reach about 1,000,000 characters before tiktoken fails; this is far below.
SEGMENT_CHARS: int = 16_384


def encode(text: str) -> list[int]:
    """Encode ``text``, special-token strings as ordinary text. Callers bound the length."""
    return _ENCODER.encode(text, disallowed_special=())


def safe_cut(text: str, start: int, limit: int) -> int:
    """Return where to end a segment that begins at ``start`` and may reach ``limit``.

    Returns ``len(text)`` when ``limit`` reaches the end. Otherwise returns the
    last position ``c`` in ``(start, limit]`` that is an exact cut, where the
    two sides' counts add up to the whole's; with none, a hard cut at ``limit``.

    Exact cuts, proved from cl100k's pre-tokeniser. It splits text into pieces
    with this pattern and encodes each piece on its own::

        '(?i:[sdmt]|ll|ve|re)|[^\\r\\n\\p{L}\\p{N}]?+\\p{L}++|\\p{N}{1,3}+
        | ?[^\\s\\p{L}\\p{N}]++[\\r\\n]*+|\\s++$|\\s*[\\r\\n]|\\s+(?!\\S)|\\s

    Each piece is the first alternative that matches where the last piece
    ended, and every character starts some alternative, so pieces are
    contiguous. Nothing looks behind, so when ``c`` is a piece boundary of the
    whole text, the pieces of ``text[c:]`` are the whole's from ``c`` on. A cut
    is therefore exact when ``c`` is a boundary of the whole and ending the
    text at ``c`` changes no piece before it. Every alternative reads at most
    one character past what it matches, with two exceptions: the first may
    read two letters after its apostrophe, and ``\\s++$``, ``\\s*[\\r\\n]``
    and ``\\s+(?!\\S)`` read to the end of the whitespace run they start in.
    Under both rules below, a space or a line break stands just before ``c``
    or at it, and the apostrophe alternative stops at either, as at the end.

    1. ``text[c]`` is a space and ``text[c - 1]`` is not whitespace.
       Boundary: a space can only be the first character of a non-whitespace
       piece (the optional ``[^\\r\\n\\p{L}\\p{N}]?+`` or `` ?``), and a
       whitespace piece cannot hold ``text[c - 1]``, so no piece holds both
       ``c - 1`` and ``c``. Prefix: no whitespace run before ``c`` reaches
       ``c``, so ``$`` is never tested at ``c``; every other alternative
       stops at a space exactly as at the end of the text (``[\\r\\n]*+``
       and the letter, digit and punctuation classes all exclude a space,
       and a space is not ``\\S``).
    2. ``text[c - 1]`` is ``\\r`` or ``\\n`` and ``text[c]`` is not whitespace.
       Boundary: ``text[c]`` ends the whitespace run, so a whitespace piece
       holding ``c - 1`` ends at ``c``: from any start in that run,
       ``\\s++$`` fails and ``\\s*[\\r\\n]`` matches to ``c``. A
       non-whitespace piece holds a line break only in its trailing
       ``[\\r\\n]*+``, which stops at ``text[c]``. Prefix: that trailing
       ``[\\r\\n]*+`` stops at the end just as at ``text[c]``; the whitespace
       piece ``[p, c)`` is matched by ``\\s++$`` instead of
       ``\\s*[\\r\\n]``, the same span and so the same tokens; and pieces
       before ``p`` read only characters before ``c``.

    "Whitespace" here is Python's ``str.isspace``, which holds for every
    character ``\\s`` matches (and for ``\\x1c`` to ``\\x1f`` too, which only
    makes rule 1 refuse a few cuts and rule 2 refuse a few more).
    """
    if limit >= len(text):
        return len(text)
    cut = start
    space = text.rfind(" ", start + 1, limit + 1)
    while space != -1:
        if not text[space - 1].isspace():
            cut = space
            break
        space = text.rfind(" ", start + 1, space)
    for line_break in ("\n", "\r"):
        found = text.rfind(line_break, cut, limit)
        while found != -1:
            if not text[found + 1].isspace():
                cut = max(cut, found + 1)
                break
            found = text.rfind(line_break, cut, found)
    return cut if cut > start else limit


def _segments(text: str, segment_chars: int) -> Iterator[str]:
    start = 0
    while start < len(text):
        end = safe_cut(text, start, start + segment_chars)
        yield text[start:end]
        start = end


def count_tokens(text: str) -> int:
    """Return the ``cl100k_base`` token count of ``text``.

    Pure function: same input always returns the same output; no side
    effects; thread-safe (the encoder is stateless). Never raises on content:
    special-token strings count as ordinary text, and long text is encoded in
    segments (see the module docstring).

    Args:
        text: The string to tokenise. Empty string returns ``0``.

    Returns:
        The number of tokens.
    """
    if len(text) <= SEGMENT_CHARS:
        return len(encode(text))
    return sum(len(encode(segment)) for segment in _segments(text, SEGMENT_CHARS))


def count_tokens_capped(text: str, cap: int) -> int:
    """Count ``text``'s tokens, stopping once the count passes ``cap``.

    Returns the exact count when it is at most ``cap``, and otherwise some
    number greater than ``cap``. Encodes one segment at a time, so a long text
    costs about one segment of work, not the whole text.
    """
    total = 0
    for segment in _segments(text, SEGMENT_CHARS):
        total += len(encode(segment))
        if total > cap:
            break
    return total
