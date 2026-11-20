"""The guard names its tables exactly, and core recognises the guard by its own words (R9-284).

A trigger function resolves unqualified names through the session's ``search_path``, so a role with
a different path, or a table of the same name earlier on it, would change which table it checked.
Core recognises a refusal by the message prefix, because the api's migrations cannot be imported
from ``persona-core``: if the two drifted, nothing would fail at a write, the refusal would simply
stop being recognised, and the memory text would go back into ``jobs.last_error``.

The live function is migration 068's (R9-311), which added the forgotten-conversation check to
062's. These read it because the behaviour needs Postgres, and that part runs in CI.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

from persona.stores import postgres

_VERSIONS = Path(__file__).resolve().parents[3] / "alembic" / "versions"
_LIVE = _VERSIONS / "068_forgotten_conversations.py"


def _migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_068_guard_contract", _LIVE)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _function_body() -> str:
    body = re.search(r"AS \$\$(.*?)\$\$", _migration().GUARD, re.DOTALL)
    assert body is not None
    return body.group(1)


def test_068_is_the_live_guard() -> None:
    """The latest migration that defines the guard function is the one these read."""
    defining = sorted(
        path.name
        for path in _VERSIONS.glob("*.py")
        if "memory_chunks_owner_guard" in path.read_text(encoding="utf-8")
    )
    assert defining[-1] == _LIVE.name


def test_every_table_in_the_function_is_schema_qualified() -> None:
    tables = set(re.findall(r"\bFROM\s+(\S+)", _function_body()))
    assert tables == {
        "public.conversations",
        "public.forgotten_conversations",
        "public.personas",
    }


def test_core_recognises_the_guard_by_what_the_guard_says() -> None:
    module = _migration()
    messages = re.findall(r"RAISE EXCEPTION\s+'([^']*)'", _function_body())
    assert len(messages) == 2, "the missing owner, and the forgotten conversation"
    assert f"{module._FUNCTION}:" == postgres._OWNER_GUARD_PREFIX  # noqa: SLF001
    assert all(m.startswith(postgres._OWNER_GUARD_PREFIX) for m in messages)  # noqa: SLF001
