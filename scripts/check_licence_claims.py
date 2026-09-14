#!/usr/bin/env python
"""No document may describe the MIT engine as source available or noncommercial.

The rule already existed in prose, inside ARCHITECTURE section 9.5: "'Source available'
describes the PolyForm packages only and must never be used of the MIT engine." It was
violated four separate times in four places anyway, twice in the same document that carries
the rule, and once in a public README brief that went to an agent. So it is a check now.

Why it matters more than tidiness: the open-core split is a licence promise. Calling the MIT
engine "source available" tells a reader they may not use it commercially, which is the
opposite of true and is the kind of wrong that costs adoption rather than a lint point.

The engine packages are MIT: core, runtime, voice. The app packages are PolyForm
Noncommercial: api, web, connectors. Verified against the LICENSE files on disk, not assumed.
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
MIT_PACKAGES = ("core", "runtime", "voice")
NC_PACKAGES = ("api", "web", "connectors")

#: Phrases that must never be applied to an engine package.
FORBIDDEN = re.compile(r"source[- ]available|noncommercial|non-commercial|polyform", re.I)

#: An engine package named close enough to a forbidden phrase to be describing it.
ENGINE = re.compile(r"persona[-_](core|runtime|voice)\b|`(core|runtime|voice)`", re.I)

#: The app half, named either by package or in plain English ("the PolyForm-NC app", "the
#: application layer"). Its presence is half of what makes a forbidden phrase legitimate.
APP = re.compile(
    r"persona[-_](api|web|connectors)\b|`(api|web|connectors)`|\bapp(lication)?\b", re.I
)

#: How near the two must be, in characters, to count as one claim about the other.
#:
#: Measured, not guessed. Across every real passage in the repo on 2026-09-14, the true
#: violations sat 8, 28 and 66 characters from the engine package they mislabelled, and the
#: correct passages that merely mention an engine later in the same paragraph sat at 105, 108
#: and 112. Eighty splits them. The one correct passage inside that range (64) is excluded by
#: the intervening-app rule in `scan_text` instead, because distance alone cannot separate 64
#: from 66.
WINDOW = 80


def packaging_metadata() -> list[str]:
    """The `license =` field is what reaches PyPI, so check that, not only the LICENSE file.

    A LICENSE file can be right while the published metadata is wrong; the index shows the
    metadata. PUBLISH_CHECKLIST.md claimed for three months that these three published as
    PolyForm Noncommercial with an Other/Proprietary classifier, which they do not.
    """
    problems: list[str] = []
    for pkg in MIT_PACKAGES + NC_PACKAGES:
        toml = ROOT / "packages" / pkg / "pyproject.toml"
        if not toml.is_file():
            continue
        declared = re.search(r"^license\s*=\s*\"([^\"]+)\"", toml.read_text(), re.M)
        if declared is None:
            continue  # no [project] table (persona-web is not a Python package)
        expected = "MIT" if pkg in MIT_PACKAGES else "PolyForm-Noncommercial-1.0.0"
        if declared.group(1) != expected:
            problems.append(
                f"packages/{pkg}/pyproject.toml declares "
                f"{declared.group(1)!r}, expected {expected!r}"
            )
    return problems


def licences_on_disk() -> dict[str, str]:
    """What each package's LICENSE actually says, so the check is grounded."""
    found = {}
    for pkg in MIT_PACKAGES + NC_PACKAGES:
        for path in (ROOT / "packages" / pkg).glob("LICENSE*"):
            head = path.read_text(errors="replace")[:400]
            found[pkg] = (
                "MIT" if "MIT License" in head else "PolyForm" if "PolyForm" in head else "?"
            )
            break
    return found


def _distance(span: tuple[int, int], other: tuple[int, int]) -> int:
    """Characters between two spans; 0 if they touch or overlap."""
    return max(0, max(span[0] - other[1], other[0] - span[1]))


def _between(span: tuple[int, int], engine: tuple[int, int], apps: list[tuple[int, int]]) -> bool:
    """Does an app package sit between this phrase and this engine mention?

    If it does, the app takes the modifier and the engine is not the referent. This is what
    separates "...(`persona-api` / `persona-web`) is separately licensed PolyForm
    Noncommercial 1.0.0 (source-available, noncommercial)" - correct, with the engine named 64
    characters earlier - from "a source-available core (`persona-core`, MIT)", which is wrong
    with nothing in between. Distance alone cannot tell those two apart.
    """
    low, high = min(span[0], engine[0]), max(span[1], engine[1])
    return any(low <= app[0] and app[1] <= high for app in apps)


def _separated(span: tuple[int, int], engine: tuple[int, int], text: str) -> bool:
    """Is there a blank line between the phrase and this engine mention?

    A blank line ends the thought, and a markdown heading always has them on both sides. The
    voice README's licence paragraph is correct, but the `## Links` section below it links to
    `persona-core` 64 characters later, which read as an attachment until this rule existed.
    """
    return "\n\n" in text[min(span[1], engine[1]) : max(span[0], engine[0])]


def scan_text(text: str) -> list[int]:
    """Line numbers where a forbidden phrase is attached to an engine package.

    Attachment is decided by nearness, which took three tries to get right:

    Line-at-a-time flagged a correct CHANGELOG paragraph because the word MIT sat on the line
    above, so the window spans lines. Then "excused if MIT appears nearby" flagged a correct
    sentence that is purely about the app half and had no reason to say MIT at all.

    What actually distinguishes the two cases is which half the phrase sits next to. "a
    source-available app (`persona-api` ...)" has the app half nearer; "persona-core
    (source-available)" has nothing but the engine near it. So: a forbidden phrase is a
    violation when an engine package is the nearest thing it could be describing.

    This is a tripwire, not a proof. A sentence that names an app package closer while still
    mislabelling the engine passes. It catches the shape that has actually happened five
    times, which is a bare claim with no distinction drawn at all.
    """
    all_engines = [m.span() for m in ENGINE.finditer(text)]
    apps = [m.span() for m in APP.finditer(text)]
    hits: list[int] = []
    for phrase in FORBIDDEN.finditer(text):
        span = phrase.span()
        engines = [e for e in all_engines if not _separated(span, e, text)]
        near_engine = min((_distance(span, e) for e in engines), default=WINDOW + 1)
        if near_engine > WINDOW:
            continue  # not close enough to any engine package to be describing one
        candidates = [e for e in engines if _distance(span, e) <= WINDOW]
        if all(_between(span, e, apps) for e in candidates):
            continue  # an app package stands between the phrase and every engine near it
        near_app = min((_distance(span, a) for a in apps), default=WINDOW + 1)
        if near_app < near_engine:
            continue  # the app half is nearer, so the phrase belongs to it
        if "never be used of the MIT engine" in text[max(0, span[0] - WINDOW) : span[1] + WINDOW]:
            continue  # the rule itself
        hits.append(text.count("\n", 0, span[0]) + 1)
    return hits


def scan(path: Path) -> list[tuple[int, str]]:
    """Scan one document, returning (line number, line text) for each violation."""
    text = path.read_text(errors="replace")
    lines = text.splitlines()
    return [(n, lines[n - 1].strip()[:150]) for n in scan_text(text)]


#: Real text, both halves. The two violations are verbatim from ARCHITECTURE before they were
#: corrected on 2026-09-14; the correct passage is verbatim from the CHANGELOG. A gate nobody
#: has watched fail is a gate nobody should trust, so it proves itself on every run.
FIXTURES: tuple[tuple[str, bool], ...] = (
    ("|           persona-core (Python library, source-available)           |", True),
    (
        "The CLI is the source-available product. People who don't want the hosted service\n"
        "can `pip install persona-core` and run everything locally with Ollama.",
        True,
    ),
    (
        "> The monorepo becomes a clean open-core project: an **MIT engine**\n"
        "> (`persona-core` / `persona-runtime` / `persona-voice`) + a **source-available\n"
        "> app** (`persona-api` / `persona-web`, PolyForm Noncommercial 1.0.0), where the",
        False,
    ),
    ("persona-api and persona-web are PolyForm Noncommercial 1.0.0.", False),
    (
        "| `persona-voice` | `packages/voice/` | wheel |\n\n"
        "`persona-api` and `persona-web` stay **private** - they are PolyForm\n"
        "Noncommercial 1.0.0 and are not part of this publication pass.",
        False,
    ),
    (
        "an `import-linter` contract proving the MIT engine never imports the "
        "PolyForm-NC app (`uv run lint-imports`).",
        False,
    ),
    ("persona-core is noncommercial, unlike persona-api.", True),
    ("> source-available core (`persona-core`, MIT) with four typed memory stores", True),
    (
        "`persona-api` is licensed under **PolyForm Noncommercial 1.0.0**. You may self-host\n"
        "it for personal, research, educational and other **noncommercial** use, but\n"
        "**commercial use requires a separate license**. The engine it composes\n"
        "(`persona-core` / `persona-runtime` / `persona-voice`) is separately **MIT**.",
        False,
    ),
    (
        "PolyForm Noncommercial 1.0.0 (source-available, noncommercial).\n\n"
        "## Links\n\n- [`persona-core`](../core/README.md)",
        False,
    ),
    (
        "It is part of the MIT licensed Open Persona engine\n"
        "(`persona-core` / `persona-runtime` / `persona-voice`); the application layer\n"
        "(`persona-api` / `persona-web`) is separately licensed\n"
        "PolyForm Noncommercial 1.0.0 (source-available, noncommercial).",
        False,
    ),
    ("persona-core, persona-runtime and persona-voice are MIT licensed.", False),
)


def selftest() -> int:
    """Fail loudly if the gate stops catching what it was built to catch."""
    broken = 0
    for text, should_flag in FIXTURES:
        if bool(scan_text(text)) != should_flag:
            verb = "missed a violation in" if should_flag else "false-positived on"
            print(f"   SELFTEST: the gate {verb}: {text.splitlines()[0][:90]}")
            broken += 1
    return broken


def main() -> int:
    if broken := selftest():
        print(f"The licence gate is not working: {broken} of {len(FIXTURES)} fixtures wrong.")
        return 1

    disk = licences_on_disk()
    wrong = {p: v for p, v in disk.items() if (p in MIT_PACKAGES) != (v == "MIT")}
    if wrong:
        print(f"LICENSE files on disk disagree with the expected map: {wrong}")
        return 1

    if metadata := packaging_metadata():
        for problem in metadata:
            print(f"   {problem}")
        return 1

    targets = [ROOT / "README.md", ROOT / "CHANGELOG.md"]
    targets += sorted((ROOT / "packages").glob("*/README.md"))
    targets += sorted((ROOT / "docs").glob("*.md")) if (ROOT / "docs").is_dir() else []

    failures = 0
    for path in targets:
        if not path.is_file():
            continue
        for number, text in scan(path):
            print(f"   {path.relative_to(ROOT)}:{number}  {text}")
            failures += 1

    print(f"Checked {len(targets)} documents against the licence map {disk}.")
    if failures:
        print(
            "\nA document describes an MIT engine package (core, runtime, voice) as source\n"
            "available, noncommercial, or PolyForm. Those words belong to api, web and\n"
            "connectors only. This has been wrong four times in four places; say MIT."
        )
        return 1
    print("Licence claim gate clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
