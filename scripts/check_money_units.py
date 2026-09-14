#!/usr/bin/env python
"""One currency, one scale, named in one place.

This product has exactly one currency and it is USD. The currency audit of 2026-09-14 found
no currency column and no rate column on any money table in the schema, so a completed charge
does not record what the customer was billed or in what currency. Until that changes there is
nothing to convert between, and a second currency appearing anywhere is a bug rather than a
feature.

The bug this exists to prevent already happened (R9-172). The constant ``10_000`` is micros
per DOLLAR. It was written as a literal in five places and as ``_MICROS_PER_KR`` in three
more, so the task surface rendered a dollar amount with a kroner label for months. Every
number was right. Only the word was wrong, and no test can fail on a word.

So this checks three things a test cannot:

1. The kroner vocabulary is absent from the money path, except where a user TYPES it.
2. Nobody re-inlines the micros-per-dollar scale as a literal.
3. The browser's copy of the constant still equals the one in core.

Point 3 is the one worth having. The two constants live in different languages, in different
packages, and no import binds them. A test in either package passes with the other wrong.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


def repo_root() -> Path:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
        )
        return Path(out.stdout.strip())
    except Exception:  # noqa: BLE001 (a git failure falls back to this file's own location)
        return Path(__file__).resolve().parent.parent


ROOT = repo_root()

#: The scale, and the two files allowed to state it.
MICROS_PER_DOLLAR = 10_000
CORE_CONSTANT = Path("packages/core/src/persona/tasks/ledger.py")
WEB_CONSTANT = Path("packages/web/src/lib/money.ts")

#: A second currency's vocabulary.
KRONER = re.compile(r"_MICROS_PER_KR\b|\bkroner\b|\bNOK\b|(?<![A-Za-z])kr(?![A-Za-z])", re.I)

#: Where money is rendered, parsed or enforced. Scanned; the rest of the repo is not.
MONEY_PATHS = (
    "packages/api/src/persona_api/approvals/budget.py",
    "packages/api/src/persona_api/billing",
    "packages/api/src/persona_api/schemas/responses.py",
    "packages/core/src/persona/billing",
    "packages/core/src/persona/tasks/ledger.py",
    "packages/core/src/persona/tools/builtin/task_introspection.py",
    "packages/runtime/src/persona_runtime/task_origination",
    "packages/web/src/components/tasks",
    "packages/web/src/components/activity/review-dateline.tsx",
    "packages/web/src/components/settings/billing-plans.tsx",
    "packages/web/src/components/settings/auto-topup-card.tsx",
    "packages/web/src/lib/money.ts",
)

#: The one deliberate exception, and why it is one.
#:
#: ``budget.py`` accepts "legg til 50kr" as INPUT and reads it as dollars with no conversion,
#: so a Norwegian user's reply is understood rather than silently unrecognised. That is a
#: parser being generous about what it accepts. Every surface that states the cap BACK says
#: dollars, so the mismatch is visible to the user in the confirmation instead of hidden
#: inside a bound. Accepting a spelling is not the same as adopting a currency.
INPUT_ONLY = ("packages/api/src/persona_api/approvals/budget.py",)


def read_constant(path: Path, pattern: str) -> int | None:
    """The integer a named constant is set to, or None if it is not there at all."""
    if not (ROOT / path).is_file():
        return None
    match = re.search(pattern, (ROOT / path).read_text())
    return int(match.group(1).replace("_", "")) if match else None


def files_to_scan() -> list[Path]:
    found: list[Path] = []
    for entry in MONEY_PATHS:
        target = ROOT / entry
        if target.is_file():
            found.append(target)
        elif target.is_dir():
            found += [
                p
                for p in sorted(target.rglob("*"))
                if p.suffix in {".py", ".ts", ".tsx"} and "test" not in p.name
            ]
    return found


def scan_text(
    text: str, *, input_only: bool, declares_scale: bool = False
) -> list[tuple[int, str]]:
    """Kroner vocabulary, and re-inlined scale literals, with their line numbers.

    ``declares_scale`` exempts the two files whose whole job is to state the number, since
    the rule is "state it in exactly these two places", not "never state it".
    """
    hits: list[tuple[int, str]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        bare = line.strip()
        if bare.startswith(("#", "*", "//", '"""', "'''")) or bare.startswith("#:"):
            continue  # prose explaining the rule is not a violation of it
        if KRONER.search(line) and not input_only:
            hits.append((number, f"kroner vocabulary: {bare[:100]}"))
        if not declares_scale and re.search(r"\b10_?000\b", line) and "micro" in line.lower():
            hits.append((number, f"micros-per-dollar re-inlined: {bare[:100]}"))
    return hits


#: Shapes that were real, so the gate proves itself rather than being taken on trust.
FIXTURES: tuple[tuple[str, bool, bool], ...] = (
    ("_MICROS_PER_KR = 10_000", False, True),
    ('return f"Spent so far: {view.spent_micros / 10_000:g}kr"', False, True),
    ("const v = micros / 10_000;", False, True),
    ("cap_micros=micros_from_dollars(cap),", False, False),
    ('return f"a spend permission, up to {format_micros(grant.cap_micros)}"', False, False),
    ("# 1 kr = 10_000 micros, the old comment", False, False),
    ('_AMOUNT = re.compile(r"(?:kr|kroner|nok)")', True, False),
)


def selftest() -> int:
    broken = 0
    for text, input_only, should_flag in FIXTURES:
        if bool(scan_text(text, input_only=input_only)) != should_flag:
            verb = "missed" if should_flag else "false-positived on"
            print(f"   SELFTEST: the gate {verb}: {text[:80]}")
            broken += 1
    return broken


def main() -> int:
    if broken := selftest():
        print(f"The money-unit gate is not working: {broken} of {len(FIXTURES)} fixtures wrong.")
        return 1

    core_pattern = r"MICROS_PER_DOLLAR:\s*Final\[int\]\s*=\s*MICROS_PER_CENT\s*\*\s*(\d+)"
    core = read_constant(CORE_CONSTANT, core_pattern)
    web = read_constant(WEB_CONSTANT, r"MICROS_PER_DOLLAR\s*=\s*([\d_]+)")
    core_value = None if core is None else core * 100
    if core_value != MICROS_PER_DOLLAR or web != MICROS_PER_DOLLAR:
        print(
            f"   the micros-per-dollar scale disagrees: core={core_value}, "
            f"web={web}, expected {MICROS_PER_DOLLAR}"
        )
        print("   These are the same number in two languages with nothing binding them.")
        return 1

    failures = 0
    for path in files_to_scan():
        rel = path.relative_to(ROOT).as_posix()
        input_only = any(rel.startswith(allowed) for allowed in INPUT_ONLY)
        declares = Path(rel) in (CORE_CONSTANT, WEB_CONSTANT)
        for number, why in scan_text(
            path.read_text(errors="replace"), input_only=input_only, declares_scale=declares
        ):
            print(f"   {rel}:{number}  {why}")
            failures += 1

    print(
        f"Checked {len(files_to_scan())} money-path files; "
        f"scale is {MICROS_PER_DOLLAR} micros per dollar."
    )
    if failures:
        print(
            "\nThe money path has exactly one currency (USD) and states its scale in exactly\n"
            "two places: persona.tasks.MICROS_PER_DOLLAR and packages/web/src/lib/money.ts.\n"
            "Import the constant, and format through format_micros / usd so the unit cannot\n"
            "be separated from the number it labels (R9-172)."
        )
        return 1
    print("Money unit gate clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
