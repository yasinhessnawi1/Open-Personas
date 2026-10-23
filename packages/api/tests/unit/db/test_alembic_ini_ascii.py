"""alembic.ini stays ASCII (Spec WIN).

Alembic reads the ini in the locale encoding. One em dash in a comment made every
migration, and so every community managed boot, fail on a Linux machine whose locale
is C (and not coerced to UTF-8). ASCII reads the same in every encoding.
"""

from __future__ import annotations

from pathlib import Path

INI = Path(__file__).resolve().parents[3] / "alembic.ini"


def test_alembic_ini_is_ascii() -> None:
    raw = INI.read_bytes()
    offenders = [n for n, line in enumerate(raw.splitlines(), 1) if any(b > 127 for b in line)]
    assert offenders == [], f"non-ASCII bytes in {INI.name} on lines {offenders}"
