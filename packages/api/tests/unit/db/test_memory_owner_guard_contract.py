"""The guard names its tables exactly, and core recognises the guard by its own words (R9-284).

A trigger function resolves unqualified names through the session's ``search_path``, so a role with
a different path, or a table of the same name earlier on it, would change which table it checked.
Core recognises a refusal by the message prefix, because the api's migrations cannot be imported
from ``persona-core``: if the two drifted, nothing would fail at a write, the refusal would simply
stop being recognised, and the memory text would go back into ``jobs.last_error``.

These read the source because the behaviour needs Postgres, and that part runs in CI.
"""

from __future__ import annotations

import re
from pathlib import Path

from persona.stores import postgres

_MIGRATION = (
    Path(__file__).resolve().parents[3] / "alembic" / "versions" / "062_memory_owner_guard.py"
)


def _function_body() -> str:
    source = _MIGRATION.read_text(encoding="utf-8")
    body = re.search(r"AS \$\$(.*?)\$\$", source, re.DOTALL)
    assert body is not None
    return body.group(1)


def test_every_table_in_the_function_is_schema_qualified() -> None:
    tables = re.findall(r"\bFROM\s+(\S+)", _function_body())
    assert sorted(tables) == ["public.conversations", "public.personas"]


def test_core_recognises_the_guard_by_what_the_guard_says() -> None:
    source = _MIGRATION.read_text(encoding="utf-8")
    name = re.search(r'^_FUNCTION = "(\w+)"', source, re.MULTILINE)
    assert name is not None
    assert "RAISE EXCEPTION '{_FUNCTION}: " in _function_body()
    assert f"{name.group(1)}:" == postgres._OWNER_GUARD_PREFIX  # noqa: SLF001
