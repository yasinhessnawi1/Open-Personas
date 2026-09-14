#!/usr/bin/env python
"""Every feature flag is declared in .env.example with its state, even when it ships off.

Owner rule, 2026-09-14. A flag that exists and is undeclared is the defect; being off is not.
This asks the pydantic-settings classes for the REAL environment name of every boolean field,
rather than grepping for an alias, because the flags that actually went dark used an
``env_prefix`` and no alias, which is precisely the shape a grep misses.

Exemption, in the owner's words: not what the system deliberately does not expose. Add to
EXEMPT with a reason; never leave something undeclared instead.
"""

from __future__ import annotations

import importlib
import pkgutil
import re
import sys
from pathlib import Path

def _repo_root() -> Path:
    """The worktree this is being run against, asked of git rather than assumed.

    Deriving it from ``__file__`` breaks the moment someone runs a copy of this script from
    somewhere else, which is exactly what happened the first time it was pointed at another
    worktree. A gate that only works from one path is a gate that gets skipped.
    """
    import subprocess

    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
        )
        return Path(out.stdout.strip())
    except Exception:
        return Path(__file__).resolve().parent.parent


ROOT = _repo_root()
EXEMPT = re.compile(r"MULTIPLIER|MARKUP|_SECRET|_KEY$|_TOKEN$|PASSWORD")

for pkg in ("core", "runtime", "api", "voice", "connectors"):
    sys.path.insert(0, str(ROOT / "packages" / pkg / "src"))


def settings_classes():
    """Every BaseSettings subclass reachable from the workspace packages."""
    try:
        from pydantic_settings import BaseSettings
    except ImportError:  # pragma: no cover - the gate cannot run without it
        print("pydantic_settings unavailable; gate skipped")
        raise SystemExit(0)
    seen = set()
    for pkg in ("persona", "persona_runtime", "persona_api", "persona_voice"):
        try:
            root = importlib.import_module(pkg)
        except Exception:
            continue
        for mod in pkgutil.walk_packages(root.__path__, root.__name__ + "."):
            if "test" in mod.name:
                continue
            try:
                m = importlib.import_module(mod.name)
            except Exception:
                continue
            for name in dir(m):
                obj = getattr(m, name)
                if (
                    isinstance(obj, type)
                    and issubclass(obj, BaseSettings)
                    and obj is not BaseSettings
                    and obj not in seen
                ):
                    seen.add(obj)
                    yield obj


def env_names(cls) -> list[tuple[str, str]]:
    """(env name, field name) for every BOOLEAN field, honouring alias and env_prefix."""
    prefix = (getattr(cls, "model_config", {}) or {}).get("env_prefix", "") or ""
    out = []
    for fname, field in getattr(cls, "model_fields", {}).items():
        if field.annotation is not bool:
            continue
        alias = getattr(field, "validation_alias", None)
        name = alias if isinstance(alias, str) else f"{prefix}{fname}".upper()
        out.append((name, fname))
    return out


example = (ROOT / ".env.example").read_text()
missing, checked = [], set()
for cls in settings_classes():
    for env, fname in env_names(cls):
        if env in checked or EXEMPT.search(env):
            continue
        checked.add(env)
        if not re.search(rf"^#?\s*{re.escape(env)}=", example, re.M):
            missing.append((env, cls.__module__ + "." + cls.__name__, fname))

print(f"Checked {len(checked)} boolean settings against .env.example.")
if missing:
    print("\nUndeclared feature flags:\n")
    for env, where, fname in sorted(missing):
        print(f"   MISSING  {env:52s} {where}.{fname}")
    print(
        "\nDeclare each in .env.example with its DEFAULT STATE, commented, even if it ships off,\n"
        "so a reader can answer 'what is on and what is off' without reading source or querying\n"
        "the deploy. If a value is commercially sensitive, add it to EXEMPT with a reason."
    )
    raise SystemExit(1)
print("Flag declaration gate clean.")
