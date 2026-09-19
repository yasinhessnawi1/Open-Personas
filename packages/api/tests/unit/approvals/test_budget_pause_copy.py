"""The budget-pause ask says what it means, in the currency the product charges (R9-172 tail).

This is the ONE message a user reads when a task stops at its cap, and it is production-wired:
``worker_root`` calls :func:`account_for_budget_pause` from the live budget gate and the persona
voices the result. The currency sweep renamed the constants, the formatters and the schema field
and left this string alone, so it still stated the money as raw ledger integers ("spent 84000 of
100000 micros") and still taught the user to answer in kroner ("add another 50kr").

The kroner half was worse than a stale label. The parser reads a bare ``50kr`` as fifty DOLLARS,
and the server refuses any single extension over ``_MAX_EXTEND_MICROS`` ($10), so a user who
followed the product's own worked example was refused by the product. A worked example has to be
one the system would actually accept.
"""

from __future__ import annotations

import re

from persona.tasks import MICROS_PER_DOLLAR
from persona_api.approvals import account_for_budget_pause, parse_extension_micros
from persona_api.routes.tasks import _MAX_EXTEND_MICROS, _extension_ceiling_detail


def _account() -> object:
    """A task that has burned $8.40 of its $10.00 cap."""
    return account_for_budget_pause("t1", cap_micros=100_000, spent_micros=84_000)


def test_the_money_is_stated_in_the_currency_the_user_is_charged_in() -> None:
    account = _account()
    assert "$8.40" in account.cause  # type: ignore[attr-defined]
    assert "$10.00" in account.cause  # type: ignore[attr-defined]


def test_the_ask_never_shows_a_raw_ledger_integer() -> None:
    """ "spent 84000 of 100000 micros" is the shape this exists to kill."""
    account = _account()
    assert "micros" not in account.cause.lower()  # type: ignore[attr-defined]
    assert "84000" not in account.cause  # type: ignore[attr-defined]
    assert "100000" not in account.cause  # type: ignore[attr-defined]


def test_the_ask_never_teaches_the_wrong_currency() -> None:
    """Every currency word the parser accepts as INPUT, refused as OUTPUT copy.

    The parser still reads ``kr`` / ``kroner`` / ``nok`` so a Norwegian user's reply is not
    silently dropped, but nothing the product PRINTS may teach a currency it does not charge in.
    """
    account = _account()
    joined = " ".join(account.options)  # type: ignore[attr-defined]
    assert not re.search(r"\b(kr|kroner|nok)\b", joined, re.IGNORECASE), joined
    assert not re.search(r"\d\s*kr\b", joined, re.IGNORECASE), joined


def test_the_worked_example_is_one_the_parser_actually_accepts() -> None:
    """A suggestion the system cannot read is worse than no suggestion."""
    account = _account()
    example = next(opt for opt in account.options if "$" in opt)  # type: ignore[attr-defined]
    parsed = parse_extension_micros(example)
    assert parsed is not None, f"the parser cannot read the example we print: {example!r}"
    assert parsed > 0


def test_the_worked_example_sits_under_the_server_ceiling() -> None:
    """The old example ("add another 50kr") parsed to $50 against a $10 ceiling, so following
    the product's own instruction earned a 422."""
    account = _account()
    example = next(opt for opt in account.options if "$" in opt)  # type: ignore[attr-defined]
    parsed = parse_extension_micros(example)
    assert parsed is not None
    assert parsed <= _MAX_EXTEND_MICROS, (
        f"the example asks for {parsed} micros against a ceiling of {_MAX_EXTEND_MICROS}"
    )


def test_the_refusal_says_the_ceiling_in_dollars_too() -> None:
    """The 422 a user hits by asking for too much was itself written in micros."""
    detail = _extension_ceiling_detail()
    assert "micros" not in detail.lower()
    assert f"${_MAX_EXTEND_MICROS // MICROS_PER_DOLLAR}" in detail
