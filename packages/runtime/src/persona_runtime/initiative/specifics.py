"""The message's specifics, checked mechanically against the cited excerpts (R9-249 HIGH-B).

The user reads the candidate's ``why_now`` and ``next_step``; the judge rules on their
claims, and this layer refuses, without a model call, the specifics it can check exactly.
It reads digits and English day and month names, as written (LOW 1: nothing else; a
number spelt out or another language's names are the judge's, which sees today's date).

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

In the OFFER only numbers are checked: "remind you on Wednesday 30 September" is a
proposal, not a claim.

Absolute dates (a5-scan-v6): the user may read a notice a day after it was written (a
held notice goes out at the next morning's flush), so a day named RELATIVE to the day
it was written is refused in both user-facing texts (:func:`relative_day_words`):

- "today", "tonight", "tomorrow", "yesterday", "this morning/afternoon/evening";
- "this" or "next" (optionally "coming") plus a weekday, "week", "weekend", "month" or
  "year"; "end of the week/month/year";
- "in N (or a, or a couple/few of) days/weeks/months/years"; "the day after/before";
- a counted countdown: "N (or a) days/weeks/fortnights/hours left, away, to go or
  remaining"; "less than", "under" or "within" a or N of those; "by/over/at the
  weekend"; "the next N days/weeks"; "overnight";
- a weekday (a full name, or a capitalised abbreviation such as "Fri") without its date,
  and a day of the month ("the 2nd", "on the 5th") without its month. A weekday is dated
  by a date beside it, in either order: "Friday 2 October", "Monday the 5th of October",
  "Friday's hearing on 2 October", "2 October (a Friday)". "Black Friday", "Good Friday"
  and the like are named days, not weekdays; "every Friday" is refused.

VAGUE hedges (also "you have time", "plenty of time", "there is time", "any day now",
"before long", "not long now", "almost due", "in the coming days", "coming up" but not
"coming up with") are refused only on a message that states NO absolute date (owner ruling
2026-10-30, F1): "soon", "shortly", "(a few, a couple of, several) days/weeks/hours
left, away, to go or remaining" (a bare "days away" too), "in a few/couple of days",
"the next few days", "still have (a few days / some time / time)", "there is still
time". On a dated message ("Your application is due on Tuesday 29 September 2026, so you
may want to finish it soon") ONE of them stays (two are refused, review LOW-3): a dated
message is never kept at the flush (:func:`time_words`), so the judge re-reads the hedge
against the day it is delivered.
:func:`relative_day_words` takes ``dated``; the caller decides it from both texts.

Not refused (fifth review LOW-2, sixth review LOW-2): a possessive period ("this
year's renewal", "next month's bill"), "in a day or two", a span measured from a stated
anchor ("within 3 days of the invoice", "within a day of the hearing"), "soon after" and
"shortly after", and "overnight" before a noun ("overnight delivery", "an overnight
train"). "I can send it over shortly" stays refused: it counts from now. They still
count as time words, so the flush re-asks the judge about them (:func:`time_words`), as
it does for any counted duration ("3 days", "a week", "a few days") the refusal does
not catch.

A date with no year is read as the most recent such date when that is at most 150 days
back, otherwise as the next one: "30 December" written on 2 January is last year's,
"2 January" written on 30 December next year's, and "Friday 3 July" written on 1
January this year's (fifth review LOW-3).

Dates are read in these forms: "2 October [2026]", "October 2 [2026]", "the 2nd of
October", ISO "2026-10-02", and day-first "02.10.2026" and "2/10/2026" (fifth review
LOW-5). Other languages' forms stay the judge's.
"""

from __future__ import annotations

import re
from datetime import date

__all__ = [
    "earliest_upcoming_date",
    "relative_day_words",
    "states_a_date",
    "vague_hedges",
    "time_words",
    "unsupported_numbers",
    "unsupported_specifics",
]

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
_COUNT = r"(?:\d+|an?|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
_SPAN = r"(?:days?|weeks?|fortnights?|months?|years?|hours?)"
_FEW = r"(?:a\s+few|a\s+couple(?:\s+of)?|few|couple(?:\s+of)?|several)"
_COUNTDOWN_UNITS = r"(?:days?|weeks?|fortnights?|hours?)"
#: A day named relative to the day the text was written (refused: the user may read it a
#: day later). A count is in digits, "a", or words up to twelve.
_RELATIVE_RE = re.compile(
    r"\b(?:today|tonight|tomorrow|yesterday"
    r"|this\s+(?:morning|afternoon|evening)"
    r"|(?:this|next)\s+(?:coming\s+)?(?:monday|tuesday|wednesday|thursday|friday|saturday"
    r"|sunday|week|weekend|month|year)"
    r"|end\s+of\s+(?:the\s+|this\s+|next\s+)?(?:week|month|year)"
    rf"|in\s+{_COUNT}\s+{_SPAN}"
    r"|the\s+day\s+(?:after|before)"
    # Counted countdowns (fifth review MEDIUM-A): true the day they are written, wrong
    # the next.
    rf"|{_COUNT}\s+(?:more\s+)?{_COUNTDOWN_UNITS}\s+(?:left|away|to\s+go|remaining)"
    rf"|(?:less\s+than|under|within)\s+{_COUNT}\s+{_COUNTDOWN_UNITS}"
    r"|(?:by|over|at)\s+the\s+weekend"
    rf"|next\s+{_COUNT}\s+{_COUNTDOWN_UNITS}"
    r"|overnight)\b",
    re.IGNORECASE,
)
#: Vague hedges (F1): refused only on a message that states no absolute date.
#: "time" ends the hedge (re-review MEDIUM-B): followed by closing punctuation, the end,
#: or a function word ("time to compare", "time left"), never "-" or a noun ("time-sensitive",
#: "time off", "time zones").
_TIME_END = (
    r"time(?=\s*[.,;:!?)]|\s*$|\s+(?:to|for|before|until|till|and|but|or|left|now|yet|"
    r"still|remains?|if|so|though|with|in|on|at|of)\b)"
)
_VAGUE_RE = re.compile(
    rf"\b(?:(?:{_FEW}\s+(?:more\s+)?)?{_COUNTDOWN_UNITS}\s+(?:left|away|to\s+go|remaining)"
    rf"|in\s+(?:a\s+)?(?:couple|few)\s+(?:of\s+)?{_SPAN}"
    rf"|next\s+{_FEW}\s+{_COUNTDOWN_UNITS}"
    r"|still\s+have\s+(?:(?:a\s+few|a\s+couple\s+of|some|enough|plenty\s+of)\s+)?"
    rf"(?:{_TIME_END}|days|weeks)"
    # Review LOW-3: more ways to say "there is time" or "it is close".
    r"|you(?:'ve|\u2019ve|\s+have)\s+(?:still\s+)?(?:got\s+)?"
    rf"(?:(?:plenty\s+of|enough|some)\s+)?{_TIME_END}"
    rf"|there\s+is\s+(?:still\s+)?(?:(?:plenty\s+of|enough|some)\s+)?{_TIME_END}"
    rf"|still\s+{_TIME_END}|plenty\s+of\s+{_TIME_END}"
    r"|any\s+day\s+now|before\s+long|not\s+long\s+now|almost\s+due"
    r"|in\s+the\s+coming\s+(?:days|weeks)"
    # Only an event that is coming up (re-review MEDIUM-B), not "keeps coming up in".
    r"|(?:is|are|['\u2019]s|['\u2019]re)\s+coming\s+up|coming\s+up\s+(?:soon|on)"
    r"|(?<!as\s)soon|shortly)\b",
    re.IGNORECASE,
)
#: Time words the refusal deliberately does not refuse (third review LOW-1): anything that
#: may read stale on another day, so the flush re-asks the judge about it. Broad on purpose,
#: and decoupled from the narrow refusal above.
_BROAD_TIME_RE = re.compile(
    r"\bcoming\s+up\b"
    r"|\b(?:have|has|had)\s+(?:(?:no|some|enough|plenty\s+of|the)\s+)?time\b"
    r"|\bthere(?:'s|\u2019s|\s+is)\s+(?:still\s+)?(?:(?:some|enough|plenty\s+of)\s+)?time\b"
    r"|\bstill\s+time\b|\btime\s+(?:left|yet|enough)\b|\benough\s+time\b"
    r"|\brunning\s+out\s+of\s+time\b"
    r"|\bnearly\b|\bclose\b|\blong\s+to\s+go\b|\baround\s+the\s+corner\b"
    r"|['\u2019]til\b",
    re.IGNORECASE,
)
#: A date the message only guesses at (B11, rulings after the second MID set): never
#: refused here, the judge decides (entailment rule 9), but never kept at the flush
#: either, so a YES on it is asked again on the new day. Narrow on purpose: "you might
#: want to", "it might be worth" and "you may be able to" are not time words.
_SPECULATIVE_DATE_RE = re.compile(
    r"\b(?:might|may|could)\s+(?:be\s+)?(?:due|fall|land)\b"
    r"|\b(?:might|may|could)\s+be\s+(?:before|after|by|around|sometime|soon)\b"
    r"|\bprobably\s+(?:due|before|after|by|around)\b"
    r"|\b(?:maybe|possibly|perhaps|unconfirmed)\b"
    r"|\bnot\s+sure\s+(?:when|if)\b"
    r"|\bI\s+think\s+(?:the|it)\b"
    r"|\bnot\s+yet\s+checked\b"
    r"|\b(?:has|have|had)(?:\s+not|n['\u2019]t)\s+(?:yet\s+)?(?:checked|confirmed|verified)\b",
    re.IGNORECASE,
)
#: Exempt from the refusal (still time words): a possessive period, "in a day or two".
_POSSESSIVE_PERIOD_RE = re.compile(r"(?:this|next)\s+(?:coming\s+)?(?:month|year)", re.I)
_POSSESSIVE_RE = re.compile(r"['\u2019]s\b")
_DAY_OR_TWO_RE = re.compile(r"in\s+an?\s+day", re.I)
_OR_TWO_RE = re.compile(r"\s+or\s+two\b", re.I)
#: A courtesy, not a countdown: "when you have time", "if you have time" (re-review
#: MEDIUM-B).
_COURTESY_TIME_RE = re.compile(r"you(?:'ve|\u2019ve|\s+have)\s", re.I)
_COURTESY_BEFORE_RE = re.compile(r"\b(?:when|whenever|if)\s+$", re.I)
_ANCHORED_SPAN_RE = re.compile(r"(?:within|under)\s", re.I)
_OF_RE = re.compile(r"\s+of\b", re.I)
_AFTER_RE = re.compile(r"\s+after\b", re.I)
#: "overnight" before a noun ("overnight delivery"): a lower-case word that is not a
#: preposition, conjunction, article or pronoun ("overnight to Bergen" counts from now).
_NOUN_AFTER_RE = re.compile(
    r"\s+(?!(?:and|or|but|nor|so|then|to|in|on|at|by|for|from|with|into|until|before|after"
    r"|if|when|while|the|a|an|your|you|it|is|was|we|i|they|he|she)\b)[a-z]+"
)
#: Any counted duration ("3 days", "a week", "a few days"): a time word, never refused
#: on its own (a duration is computed, the judge's to check).
_COUNTED_SPAN_RE = re.compile(rf"\b(?:{_COUNT}|{_FEW})\s+(?:more\s+)?{_SPAN}\b", re.IGNORECASE)
#: A weekday as written: a full name in any case, an abbreviation capitalised ("Fri",
#: "FRI"; lower-case "sat" or "wed" is an ordinary word).
_WEEKDAY_TOKEN_RE = re.compile(
    r"\b(?:(?i:monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
    r"|Mon|Tues?|Wed|Thu(?:rs?)?|Fri|Sat|Sun|MON|TUE|WED|THU|FRI|SAT|SUN)\b"
)
#: A named day that is not a weekday reference ("Black Friday", "Good Friday").
_NAMED_DAY_RE = re.compile(r"\b(?:black|good|cyber|easter|ash|maundy|holy|palm)\s+$", re.I)
#: Between a weekday and the date after it: "Friday 2 October", "Fri. 2 Oct", "Monday
#: the 5th of October", "Friday's hearing on 2 October" (two words at most, and not
#: "and"/"or": "Friday or 2 October" names two days).
_WEEKDAY_THEN_DATE_RE = re.compile(
    r"\.?(?:['\u2019]s)?\s*,?\s*(?:(?!(?:and|or|but|not|nor)\b)[A-Za-z]+\s+){0,2}"
)
#: Between a date and the weekday after it: "2 October, a Friday", "2 October (a
#: Friday)", "4 October 2026, Sunday".
_DATE_THEN_WEEKDAY_RE = re.compile(
    r"[\s,]*\(?\s*(?:(?:a|an|which\s+is\s+a|that\s+is\s+a)\s+)?", re.I
)
#: A month abbreviation as written, capitalised ("Oct", "OCT"), anywhere.
_MONTH_ABBR_TOKEN_RE = re.compile(
    r"\b(?:Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept?|Oct|Nov|Dec|JAN|FEB|MAR|APR|JUN|JUL|AUG|SEPT?"
    r"|OCT|NOV|DEC)\b"
)
#: A day of the month named without its month ("the 2nd", "on the 5th"); an ordinal
#: before a noun ("the 3rd time") is not a day.
_ORDINAL_DAY_RE = re.compile(
    r"\b(?:the|on)\s+\d{1,2}(?:st|nd|rd|th)\b"
    r"(?!\s+(?!(?:at|and|or|by|before|after|until|to|in|from|when|if|of)\b)[a-z])",
    re.I,
)
_MAY_CONTEXT_RE = re.compile(r"\b(?:in|on|by|until|of)\s+$", re.IGNORECASE)

_MONTH_NAMES = "|".join(_MONTHS) + "|" + "|".join(_MONTH_ABBR)
#: How far back a yearless date may still read as past; further back reads forward.
_PAST_HORIZON_DAYS = 150
_DATE_PATTERNS = (
    re.compile(
        rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MONTH_NAMES})\.?(?:,?\s+(\d{{4}}))?\b",
        re.I,
    ),
    re.compile(rf"\b({_MONTH_NAMES})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(\d{{4}}))?\b", re.I),
    re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"),
    re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b"),
    re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b"),
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


def _yearless(month: int, day: int, today: date) -> date | None:
    """A date written without its year: the most recent one when it is at most
    ``_PAST_HORIZON_DAYS`` back, otherwise the next one on or after ``today``."""
    years = (today.year - 1, today.year, today.year + 1)
    dates = [d for year in years if (d := _safe_date(year, month, day)) is not None]
    recent = max((d for d in dates if d < today), default=None)
    ahead = min((d for d in dates if d >= today), default=None)
    if recent is not None and (ahead is None or (today - recent).days <= _PAST_HORIZON_DAYS):
        return recent
    return ahead


def _stated_dates(text: str, today: date) -> list[date]:
    """Every calendar date the text states (day + month, with or without a year)."""
    found: list[date] = []
    for match in _DATE_PATTERNS[0].finditer(text):
        month = _month_number(match[2])
        if month is None:
            continue
        stated = (
            _safe_date(int(match[3]), month, int(match[1]))
            if match[3]
            else _yearless(month, int(match[1]), today)
        )
        if stated:
            found.append(stated)
    for match in _DATE_PATTERNS[1].finditer(text):
        month = _month_number(match[1])
        if month is None:
            continue
        stated = (
            _safe_date(int(match[3]), month, int(match[2]))
            if match[3]
            else _yearless(month, int(match[2]), today)
        )
        if stated:
            found.append(stated)
    for match in _DATE_PATTERNS[2].finditer(text):
        if d := _safe_date(int(match[1]), int(match[2]), int(match[3])):
            found.append(d)
    for pattern in _DATE_PATTERNS[3:]:  # day first: "02.10.2026", "2/10/2026"
        for match in pattern.finditer(text):
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


def _date_spans(text: str) -> list[tuple[int, int]]:
    return [match.span() for pattern in _DATE_PATTERNS for match in pattern.finditer(text)]


def _weekday_is_dated(text: str, start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    """A weekday with a date beside it, in either order, or a named day ("Good Friday")."""
    if _NAMED_DAY_RE.search(text[:start]):
        return True
    for span_start, span_end in spans:
        if span_start >= end and _WEEKDAY_THEN_DATE_RE.fullmatch(text, end, span_start):
            return True
        if span_end <= start and _DATE_THEN_WEEKDAY_RE.fullmatch(text, span_end, start):
            return True
    return False


def _exempt(text: str, match: re.Match[str]) -> bool:
    """A refused-shape phrase that is not relative to the writing day (the module's
    "Not refused" list): it stays a time word."""
    phrase, end = match[0].lower(), match.end()
    if _COURTESY_TIME_RE.match(phrase) and _COURTESY_BEFORE_RE.search(text[: match.start()]):
        return True
    if _POSSESSIVE_PERIOD_RE.fullmatch(phrase):
        return _POSSESSIVE_RE.match(text, end) is not None
    if _DAY_OR_TWO_RE.fullmatch(phrase):
        return _OR_TWO_RE.match(text, end) is not None
    if _ANCHORED_SPAN_RE.match(phrase):
        return _OF_RE.match(text, end) is not None
    if phrase in ("soon", "shortly"):
        return _AFTER_RE.match(text, end) is not None
    if phrase == "overnight":
        return _NOUN_AFTER_RE.match(text, end) is not None
    return False


def states_a_date(*texts: str) -> bool:
    """Whether any of ``texts`` states an absolute calendar date (any form read here)."""
    return any(_date_spans(text) for text in texts)


def vague_hedges(*texts: str) -> list[str]:
    """The vague hedges ("soon", "a few days left", "still have time") the texts carry
    (the exempt forms excluded); allowed on a dated message, so the digest, which reads a
    held notice without the judge, checks them against the date itself."""
    return [m[0] for text in texts for m in _VAGUE_RE.finditer(text) if not _exempt(text, m)]


def relative_day_words(text: str, *, dated: bool = False) -> list[str]:
    """The days ``text`` names relative to when it was written (refused in user text),
    in the order written; a weekday inside a phrase ("this Friday") is listed once.

    ``dated``: the message states an absolute date (:func:`states_a_date`), so its vague
    hedges are allowed (F1); day-naming and counted forms are refused either way.
    """
    matches = [m for m in _RELATIVE_RE.finditer(text) if not _exempt(text, m)]
    if not dated:  # a vague hedge inside a counted form ("two days left") is listed once
        taken = [m.span() for m in matches]
        matches += [
            m
            for m in _VAGUE_RE.finditer(text)
            if not _exempt(text, m) and not any(s < m.end() and m.start() < e for s, e in taken)
        ]
    found = [(match.start(), match[0]) for match in matches]
    phrases = [match.span() for match in matches]
    spans = _date_spans(text)
    for match in _WEEKDAY_TOKEN_RE.finditer(text):
        inside = any(start <= match.start() < end for start, end in phrases)
        if not inside and not _weekday_is_dated(text, match.start(), match.end(), spans):
            found.append((match.start(), match[0]))
    for match in _ORDINAL_DAY_RE.finditer(text):
        if not any(start < match.end() and match.start() < end for start, end in spans):
            found.append((match.start(), match[0]))
    return [word for _, word in sorted(found)]


def time_words(text: str) -> list[str]:
    """Every word or phrase in ``text`` that places it in time: a relative phrase or an
    undated day (:func:`relative_day_words`, the exempt ones included), any weekday or
    month name (dated or not, full or abbreviated), any stated date, and any counted
    duration (fifth review MEDIUM-A). The flush re-asks the judge about a message that
    has any (fourth review MEDIUM-1 (b)), so it is read against the day it is delivered,
    not the day it was written."""
    tokens = (
        _RELATIVE_RE,
        _VAGUE_RE,
        _WEEKDAY_TOKEN_RE,
        _MONTH_ABBR_TOKEN_RE,
        _ORDINAL_DAY_RE,
        _COUNTED_SPAN_RE,
        _BROAD_TIME_RE,
        _SPECULATIVE_DATE_RE,
    )
    found = [match[0] for pattern in tokens for match in pattern.finditer(text)]
    found += [text[start:end] for start, end in _date_spans(text)]
    found += [word for word, _ in _calendar_words(text)[0]]
    return list(dict.fromkeys(found))


def earliest_upcoming_date(*texts: str, written_on: date) -> date | None:
    """The earliest calendar date the texts state on or after the day they were written
    (``written_on``, which also places a yearless date); ``None`` when they state none.

    The held notice's lapse anchor (fourth review LOWs 3 and 4): the message and the
    offer both count, and a date already past when written ("dropped on 25 September")
    is history, not the moment the notice is about.
    """
    dates = [d for text in texts for d in _stated_dates(_normalise(text), written_on)]
    upcoming = [d for d in dates if d >= written_on]
    return min(upcoming) if upcoming else None
