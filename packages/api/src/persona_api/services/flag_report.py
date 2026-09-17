"""What is switched on, said once at boot (R9-170, sweep part2 N).

The flag register was built by hand on 2026-09-14 because nothing in the running process
could answer "what is on". Production set three feature flags and everything else ran on
the code default, and there was no boot line, no endpoint, no artifact that said which
was which. The only way to find out was to read the code defaults, list the Fly secrets
and reason about the gap. Local and production had been diverging silently for months,
so every operator pass run locally was passing a configuration production did not have.

This is the boot line. It reports every boolean setting of every pydantic-settings class
the process has ALREADY imported by the time the lifespan runs, so it adds no imports and
no side effects of its own, plus the three scale backends (rate limit, audit, storage)
that are strings rather than booleans and escaped the register for exactly that reason.

It cannot print a secret or a commercially sensitive number: values are reduced to
``on`` / ``off`` / ``unset`` (or a short backend name), and any field whose environment
name looks like a key, token, password, markup or multiplier is excluded by the same
pattern the flag-declaration gate in ``scripts/check_flags_declared.py`` uses.
"""

from __future__ import annotations

import re
import sys
import types
from typing import TYPE_CHECKING, Any, Union, get_args, get_origin

from persona.logging import get_logger

if TYPE_CHECKING:
    from persona_api.config import APIConfig

__all__ = ["EXEMPT", "SCALE_BACKENDS", "effective_flags", "log_effective_flags"]

_log = get_logger("api.flags")

#: Names that must never appear in the report, whatever their type. Mirrors the gate.
EXEMPT = re.compile(r"MULTIPLIER|MARKUP|_SECRET|_KEY$|_TOKEN$|PASSWORD")

#: The three R9-170 backends: string settings whose default is the single-machine option
#: and which must move BEFORE the machine count does. They are reported by field name on
#: the api config, because a register that only looks for booleans never saw them.
SCALE_BACKENDS: tuple[str, ...] = ("rate_limit_backend", "audit_backend", "storage_backend")


def _is_bool_field(annotation: Any) -> bool:  # noqa: ANN401 (a type annotation, any shape)
    """``bool`` or ``bool | None``; nothing else counts as a flag."""
    if annotation is bool:
        return True
    origin = get_origin(annotation)
    if origin is Union or origin is types.UnionType:
        return set(get_args(annotation)) == {bool, type(None)}
    return False


def _env_name(cls: type, field_name: str, field: Any) -> str:  # noqa: ANN401 (FieldInfo)
    """The real environment name, honouring a string alias and the class's ``env_prefix``."""
    alias = getattr(field, "validation_alias", None)
    if isinstance(alias, str):
        return alias
    config: dict[str, Any] = getattr(cls, "model_config", {}) or {}
    prefix = config.get("env_prefix", "") or ""
    return f"{prefix}{field_name}".upper()


def _settings_classes_already_imported() -> list[type]:
    """Every pydantic-settings class in ``sys.modules`` right now, and nothing more.

    Deliberately not a package walk: importing modules at boot to find flags would be a
    side effect in a function whose whole point is to have none, and the flags the process
    actually reads are, by construction, in modules it has already imported.
    """
    try:
        from pydantic_settings import BaseSettings
    except ImportError:  # pragma: no cover - the api cannot start without it either
        return []
    found: list[type] = []
    seen: set[type] = set()
    for module in list(sys.modules.values()):
        # The namespace dict, not dir()+getattr: dir() plus attribute access fires every
        # module-level __getattr__ (lazy imports, deprecation shims) in the whole process,
        # which is exactly the side effect this function promises not to have.
        namespace = getattr(module, "__dict__", None) if module is not None else None
        if not isinstance(namespace, dict):
            continue
        for obj in list(namespace.values()):
            if (
                isinstance(obj, type)
                and issubclass(obj, BaseSettings)
                and obj is not BaseSettings
                and obj not in seen
            ):
                seen.add(obj)
                found.append(obj)
    return found


def _instance(cls: type, config: APIConfig | None) -> Any:  # noqa: ANN401 (a settings object)
    """The live instance for ``cls``: the api's own config when that is what it is, else a
    fresh read of the environment. ``None`` when the class cannot be built from the
    environment alone (required fields), which is then simply not reported."""
    if config is not None and isinstance(config, cls):
        return config
    try:
        return cls()
    except Exception:  # noqa: BLE001 (required fields, invalid env; not this report's problem)
        return None


def effective_flags(config: APIConfig | None = None) -> list[tuple[str, str]]:
    """``(environment name, state)`` for every reportable flag, sorted, deduplicated.

    State is ``on`` / ``off`` / ``unset`` for booleans; for the scale backends it is the
    backend's own short name (``memory``, ``postgres``, ``jsonl``, ``local``, ``s3``).
    """
    report: dict[str, str] = {}
    for cls in _settings_classes_already_imported():
        instance = _instance(cls, config)
        if instance is None:
            continue
        for field_name, field in getattr(cls, "model_fields", {}).items():
            if not _is_bool_field(field.annotation):
                continue
            env = _env_name(cls, field_name, field)
            if env in report or EXEMPT.search(env):
                continue
            value = getattr(instance, field_name, None)
            report[env] = "unset" if value is None else ("on" if value else "off")
    if config is not None:
        for field_name in SCALE_BACKENDS:
            field = type(config).model_fields.get(field_name)
            if field is None:
                continue
            env = _env_name(type(config), field_name, field)
            report[env] = str(getattr(config, field_name))
    return sorted(report.items())


def log_effective_flags(config: APIConfig | None = None) -> None:
    """One INFO line at boot: the counts, then every flag with its state.

    One line, not one per flag, so it is greppable as a unit and cannot interleave with
    the rest of startup. Read it with ``grep "feature flags at boot"``.
    """
    flags = effective_flags(config)
    on = sum(1 for _, state in flags if state == "on")
    off = sum(1 for _, state in flags if state == "off")
    unset = sum(1 for _, state in flags if state == "unset")
    _log.info(
        "feature flags at boot: {on} on, {off} off, {unset} unset | {flags}",
        on=on,
        off=off,
        unset=unset,
        flags=" ".join(f"{name}={state}" for name, state in flags),
    )
