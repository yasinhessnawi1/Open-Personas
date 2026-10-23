"""The message's specifics, checked mechanically against the cited excerpts (R9-249 HIGH-B).

The user reads the candidate's ``why_now`` and ``next_step``; the judge rules on their
claims, and this layer refuses, without a model call, the specifics it can check exactly.
It reads digits and English day and month names, as written (LOW 1: nothing else; a
number spelt out, another language's names, a relative phrase like "next week" are the
judge's, which sees today's date).

Normalised first, in both texts, so an honest rewording is never refused:

- thousands separators between 3-digit groups are dropped (",", ".", a space, a no-break
  or thin space): "8,400 kr", "8 400 kr" and "8400 kr" are one number;
- a time "10:00" reads as "10" ("10am" and "10 am" already read as "10": numbers
  are whole digit runs);
- a number that counts a duration ("in 3 days", "2 weeks") is left to the judge: it is
  computed, not quoted.

Then, in the MESSAGE:

- every remaining number must appear in the excerpts (whole digit runs: "1" is not
  found inside "10"), or be the day, month or year of a date they state;
- a month (a full name; "May" only next to a digit or after in/on/by/until/of; a
  three-letter abbreviation only next to a digit) must be named by the excerpts or be
  the month of a date they state;
- a weekday (a full name; an abbreviation only next to a digit) is supported when the
  excerpts name it or state a date that falls on it (the year from the date itself, or
  today's). When the excerpts state dates and none falls on it, it is refused; when they
  state no date at all, it is left to the judge.

In the OFFER only numbers are checked: "remind you on Wednesday" is a proposal, not a
claim.
"""

from __future__ import annotations

import re
from datetime import date

__all__ = ["unsupported_numbers", "unsupported_specifics"]

_MONTHS = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)  # fmt: skip
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_MONTH_ABBR = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}  # fmt: skip
_DAY_ABBR = {
    "mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3, "thurs": 3,
    "fri": 4, "sat": 5, "sun": 6,
}  # fmt: skip

_SEPARATOR_RE = re.compile(r"(?<=\d)[,.\u00a0\u202f\u2009 ](?=\d{3}(?!\d))")
_CLOCK_RE = re.compile(r"\b(\d{1,2}):00\b")
_NUMBER_RE = re.compile(r"(?<!\d)\d+(?!\d)")
_DURATION_RE = re.compile(
    r"\s*(?:day|days|week|weeks|hour|hours|minute|minutes|month|months|year|years)\b",
    re.IGNORECASE,
)
_WORD_RE = re.compile(r"[A-Za-z]+")
_MAY_CONTEXT_RE = re.compile(r"\b(?:in|on|by|until|of)\s+$", re.IGNORECASE)

_MONTH_NAMES = "|".join(_MONTHS) + "|" + "|".join(_MONTH_ABBR)
_DATE_PATTERNS = (
    re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH_NAMES})\.?(?:,?\s+(\d{{4}}))?\b", re.I),
    re.compile(rf"\b({_MONTH_NAMES})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(\d{{4}}))?\b", re.I),
    re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"),
    re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b"),
)


def _normalise(text: str) -> str:
    text = _SEPARATOR_RE.sub("", text)
    return _CLOCK_RE.sub(r"\1", text)


def _month_number(word: str) -> int | None:
    lower = word.lower()
    if lower in _MONTHS:
        return _MONTHS.index(lower) + 1
    return _MONTH_ABBR.get(lower)


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _stated_dates(text: str, today: date) -> list[date]:
    """Every calendar date the text states (day + month, with or without a year)."""
    found: list[date] = []
    for match in _DATE_PATTERNS[0].finditer(text):
        month = _month_number(match[2])
        year = int(match[3]) if match[3] else today.year
        if month is not None and (d := _safe_date(year, month, int(match[1]))):
            found.append(d)
    for match in _DATE_PATTERNS[1].finditer(text):
        month = _month_number(match[1])
        year = int(match[3]) if match[3] else today.year
        if month is not None and (d := _safe_date(year, month, int(match[2]))):
            found.append(d)
    for match in _DATE_PATTERNS[2].finditer(text):
        if d := _safe_date(int(match[1]), int(match[2]), int(match[3])):
            found.append(d)
    for match in _DATE_PATTERNS[3].finditer(text):
        if d := _safe_date(int(match[3]), int(match[2]), int(match[1])):
            found.append(d)
    return found


def _next_to_digit(text: str, start: int, end: int) -> bool:
    before = text[max(0, start - 2) : start]
    after = text[end : end + 3]
    return bool(re.search(r"\d\s?$", before) or re.match(r"\.?\s?\d", after))


def _calendar_words(text: str) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    """(months, weekdays) the text names, as (word, number), by the rules above."""
    months: list[tuple[str, int]] = []
    days: list[tuple[str, int]] = []
    for match in _WORD_RE.finditer(text):
        word, lower = match[0], match[0].lower()
        near_digit = _next_to_digit(text, match.start(), match.end())
        if lower == "may":
            if near_digit or _MAY_CONTEXT_RE.search(text[: match.start()]):
                months.append((word, 5))
        elif lower in _MONTHS:
            months.append((word, _MONTHS.index(lower) + 1))
        elif lower in _MONTH_ABBR and near_digit:
            months.append((word, _MONTH_ABBR[lower]))
        elif lower in _WEEKDAYS:
            days.append((word, _WEEKDAYS.index(lower)))
        elif lower in _DAY_ABBR and near_digit:
            days.append((word, _DAY_ABBR[lower]))
    return months, days


def unsupported_numbers(
    message: str, excerpts: list[str], *, also: frozenset[str] = frozenset()
) -> list[str]:
    """The numbers in ``message`` no excerpt states (normalised; durations excluded).

    ``also`` are numbers the excerpts state in another form (a stated date's day, month
    and year, so "2026-10-04" supports "4 October").
    """
    text = _normalise("\n".join(excerpts))
    normal = _normalise(message)
    missing = []
    for match in _NUMBER_RE.finditer(normal):
        if _DURATION_RE.match(normal, match.end()):
            continue  # a computed duration ("in 3 days") is the judge's
        if match[0] not in also and not re.search(rf"(?<!\d){match[0]}(?!\d)", text):
            missing.append(match[0])
    return missing


def unsupported_specifics(message: str, excerpts: list[str], *, today: date) -> list[str]:
    """The numbers, months and weekdays in ``message`` the excerpts do not support."""
    text = "\n".join(excerpts)
    dates = _stated_dates(_normalise(text), today)
    parts = frozenset(str(v) for d in dates for v in (d.day, d.month, d.year))
    missing = unsupported_numbers(message, excerpts, also=parts)
    named_months, named_days = _calendar_words(text)
    months = {m for _, m in named_months} | {d.month for d in dates}
    weekdays = {w for _, w in named_days} | {d.weekday() for d in dates}
    said_months, said_days = _calendar_words(message)
    missing.extend(word for word, month in said_months if month not in months)
    if dates:  # with no stated date, a weekday is the judge's to verify
        missing.extend(word for word, day in said_days if day not in weekdays)
    return missing
