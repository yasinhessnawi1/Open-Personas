"""Every background op hands its failed tries to the biller (D-ORCH-11-18-s).

``bill_background_llm`` charges an op's failed tries that cost tokens only when the op
passes them, from its usage sink's ``UsageTotals.failed_tries``. A call site that left
the keyword out would quietly stop charging them, so this pins every call site.
"""

from __future__ import annotations

import re
from pathlib import Path

_SRC = Path(__file__).resolve().parents[3] / "src" / "persona_api"
_DEFINITION = _SRC / "services" / "background_billing.py"


def _call_sites() -> list[tuple[Path, str]]:
    sites: list[tuple[Path, str]] = []
    for path in sorted(_SRC.rglob("*.py")):
        if path == _DEFINITION:
            continue
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"\bbill_background_llm\(\n(.*?)\n\s*\)", text, re.S):
            sites.append((path, match.group(1)))
    return sites


def test_the_background_ops_are_found() -> None:
    assert len(_call_sites()) >= 7


def test_every_background_op_passes_its_failed_tries() -> None:
    missing = [
        str(path.relative_to(_SRC))
        for path, args in _call_sites()
        if not re.search(r"\bfailed_tries=\w+\.failed_tries\b", args)
    ]
    assert missing == []
