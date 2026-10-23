#!/usr/bin/env python
"""Render the Hetzner env files from the templates and the owner's local .env.

Runs on the OWNER'S PC, never on the box and never in CI. First, the inventory, which
writes nothing and lists every name the render would take from .env, whether .env has a
value for it, and where it goes, so the owner can mark which values are dev:

    uv run python -I deploy/hetzner/render_env.py --dotenv .env --inventory

Then the render:

    uv run python -I deploy/hetzner/render_env.py --dotenv .env --out <dir outside the repo> \\
        [--livekit selfhosted|cloud] [--chains-from-dotenv]

``--livekit`` defaults to ``selfhosted`` (owner ruling 2026-10-23: LiveKit runs on the box;
LiveKit Cloud's free plan is the one-setting fallback, ``--livekit cloud``).

For every ``deploy/hetzner/env/<name>.env.example`` it writes ``<out>/<name>.env`` (mode
0600 where the OS supports it). Values are resolved by the template tokens documented in
``env/common.env.example``. Tokens it cannot resolve stay in the file, and
``bin/preflight.sh`` on the box refuses to start while any remain:

* ``__GEN_<NAME>__`` stays for ``bin/fill_generated.sh`` on the box (by design).
* ``__OWNER__``, ``__OWNER_CLERK_PROD__`` (Clerk values: .env holds the development
  instance's, so they are never copied) and an unconfirmed ``__GITHUB_PRODUCTION__`` stay for
  the owner to fill in.
* ``__MISSING_FROM_DOTENV__`` replaces a ``__FROM_ENV__`` whose name .env lacks.

It NEVER prints a value: output is setting names and fixed states, the same rule as
``scripts/validate_deploy_chains.py``. It refuses an output directory inside this
repository's checkout, so a rendered file cannot be committed by accident.

Values are written single-quoted (Compose takes them literally, multi-line PEMs included).
A value holding a single quote is written double-quoted with ``\\``, ``"`` and ``$``
escaped, so Compose's interpolation never rewrites it.
"""

from __future__ import annotations

import argparse
import contextlib
import re
import subprocess
import sys
from pathlib import Path

KIT_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = KIT_DIR / "env"
TEMPLATE_SUFFIX = ".env.example"

FROM_ENV = "__FROM_ENV__"
FROM_ENV_OTHER = re.compile(r"^__FROM_ENV:([A-Z][A-Z0-9_]*)__$")
GITHUB_PRODUCTION = "__GITHUB_PRODUCTION__"
OWNER = "__OWNER__"
# Clerk-derived values: .env holds the DEVELOPMENT instance's (owner, 2026-10-23), so these
# are never copied; the owner pastes the production instance's values.
OWNER_CLERK = "__OWNER_CLERK_PROD__"
MISSING = "__MISSING_FROM_DOTENV__"
GENERATED = re.compile(r"__GEN_[A-Z0-9_]+__")
LIVEKIT_TOKENS = ("__LIVEKIT_URL__", "__LIVEKIT_API_KEY__", "__LIVEKIT_API_SECRET__")

SELFHOSTED_LIVEKIT = {
    "__LIVEKIT_URL__": "wss://livekit.openpersonasai.com",
    "__LIVEKIT_API_KEY__": "__GEN_LIVEKIT_API_KEY__",
    "__LIVEKIT_API_SECRET__": "__GEN_LIVEKIT_API_SECRET__",
}
# In cloud mode the Cloud credentials come from these .env names (no setting reads them
# today; the owner confirms they hold the LiveKit Cloud project's values).
CLOUD_LIVEKIT_SOURCES = {
    "__LIVEKIT_URL__": "PERSONA_LIVEKIT_URL",
    "__LIVEKIT_API_KEY__": "PERSONA_LIVEKIT_API_KEY",
    "__LIVEKIT_API_SECRET__": "PERSONA_LIVEKIT_API_SECRET",
}

_LINE = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")


def parse_template(path: Path) -> list[tuple[str, str]]:
    """The ``NAME=value`` pairs of a template, in order. Comments and blanks are skipped."""
    pairs: list[tuple[str, str]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _LINE.match(line)
        if match is None:
            raise SystemExit(f"render_env: unparseable template line in {path.name}")
        pairs.append((match.group(1), match.group(2)))
    return pairs


def load_dotenv(path: Path) -> dict[str, str]:
    """The owner's .env, parsed the way the app's own loader parses it."""
    from dotenv import dotenv_values

    return {k: v for k, v in dotenv_values(path).items() if v is not None}


def resolve(
    name: str,
    template_value: str,
    dotenv: dict[str, str],
    *,
    livekit: str,
    chains_from_dotenv: bool,
) -> tuple[str, str]:
    """Resolve one template value. Returns ``(value, state)``; the state is safe to print."""
    if template_value == FROM_ENV:
        return _copy(name, dotenv)
    other = FROM_ENV_OTHER.match(template_value)
    if other is not None:
        value, state = _copy(other.group(1), dotenv)
        return value, f"{state} (from {other.group(1)})"
    if template_value == GITHUB_PRODUCTION:
        if chains_from_dotenv:
            value, state = _copy(name, dotenv)
            return value, f"{state} (owner confirmed .env holds the production value)"
        return template_value, "owner: GitHub Production value"
    if template_value in LIVEKIT_TOKENS:
        if livekit == "selfhosted":
            value = SELFHOSTED_LIVEKIT[template_value]
            if GENERATED.search(value):
                return value, "generated on the box"
            return value, "fixed (selfhosted)"
        value, state = _copy(CLOUD_LIVEKIT_SOURCES[template_value], dotenv)
        return value, f"{state} (cloud, from {CLOUD_LIVEKIT_SOURCES[template_value]})"
    if template_value == OWNER:
        return template_value, "owner must supply"
    if template_value == OWNER_CLERK:
        return template_value, "owner: production Clerk instance (never copied from .env)"
    if GENERATED.search(template_value):
        return template_value, "generated on the box"
    return template_value, "fixed"


def _copy(source: str, dotenv: dict[str, str]) -> tuple[str, str]:
    value = dotenv.get(source)
    if value is None or value.strip() == "":
        return MISSING, "MISSING from .env"
    return value, "copied from .env"


def quote(value: str) -> str:
    """A Compose env_file value that Compose reads back exactly as ``value``."""
    if "'" not in value:
        return f"'{value}'"
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "$$")
    escaped = escaped.replace("\n", "\\n")
    return f'"{escaped}"'


def repo_root() -> Path:
    """The checkout this kit lives in, asked of git, falling back to the kit's location."""
    try:
        result = subprocess.run(
            ["git", "-C", str(KIT_DIR), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return KIT_DIR.parents[1]
    if result.returncode != 0 or not result.stdout.strip():
        return KIT_DIR.parents[1]
    return Path(result.stdout.strip()).resolve()


def inside_checkout(path: Path) -> bool:
    """True when ``path`` is inside this repository's checkout (where a commit could take it)."""
    return path.resolve().is_relative_to(repo_root())


def render(
    dotenv: dict[str, str], out: Path, *, livekit: str, chains_from_dotenv: bool
) -> list[tuple[str, str, str]]:
    """Write every env file. Returns ``(file, name, state)`` rows for the report."""
    report: list[tuple[str, str, str]] = []
    out.mkdir(parents=True, exist_ok=True)
    for template in sorted(TEMPLATE_DIR.glob(f"*{TEMPLATE_SUFFIX}")):
        target = out / template.name.removesuffix(".example")
        lines = [f"# Rendered from deploy/hetzner/env/{template.name}. Secret: mode 0600."]
        for name, template_value in parse_template(template):
            value, state = resolve(
                name, template_value, dotenv, livekit=livekit, chains_from_dotenv=chains_from_dotenv
            )
            lines.append(f"{name}={quote(value)}")
            report.append((target.name, name, state))
        target.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        # Windows ignores most of the mode; the box's fill_generated.sh sets 0600 again.
        with contextlib.suppress(OSError):
            target.chmod(0o600)
    return report


def _presence(source: str, dotenv: dict[str, str]) -> str:
    if source not in dotenv:
        return "absent"
    return "set" if dotenv[source].strip() else "empty"


def inventory(dotenv: dict[str, str]) -> list[tuple[str, str, str, str]]:
    """Every name the render would take from .env, BEFORE anything is written.

    Returns ``(source name in .env, presence, destination file, destination name)`` rows.
    Presence is ``set``, ``empty`` or ``absent``: never the value. The seven shared settings
    and both LiveKit Cloud sources are listed too, because ``--chains-from-dotenv`` and
    ``--livekit cloud`` would copy them.
    """
    rows: list[tuple[str, str, str, str]] = []
    for template in sorted(TEMPLATE_DIR.glob(f"*{TEMPLATE_SUFFIX}")):
        target = template.name.removesuffix(".example")
        for name, template_value in parse_template(template):
            other = FROM_ENV_OTHER.match(template_value)
            if template_value in (FROM_ENV, GITHUB_PRODUCTION):
                source = name
            elif other is not None:
                source = other.group(1)
            elif template_value in CLOUD_LIVEKIT_SOURCES:
                source = CLOUD_LIVEKIT_SOURCES[template_value]
            else:
                continue
            rows.append((source, _presence(source, dotenv), target, name))
    return rows


def print_inventory(rows: list[tuple[str, str, str, str]]) -> None:
    """A checklist the owner marks prod or dev per name. Names and presence only."""
    width = max(len(source) for source, *_ in rows)
    print("# Mark each line [p] production or [d] dev (then supply the production value).")
    print("# Presence only: this never shows a value.\n")
    for source, presence, target, name in rows:
        destination = target if source == name else f"{target} as {name}"
        print(f"[ ] {source:<{width}}  .env: {presence:<6}  -> {destination}")
    counts = {p: sum(1 for _, q, *_ in rows if q == p) for p in ("set", "empty", "absent")}
    print(
        f"\n{len(rows)} names: {counts['set']} set, {counts['empty']} empty, "
        f"{counts['absent']} absent in .env."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dotenv", type=Path, required=True)
    parser.add_argument(
        "--inventory",
        action="store_true",
        help="write nothing; list every name the render would take from .env, whether .env "
        "has a value for it, and its destination file (names only, never values)",
    )
    parser.add_argument("--out", type=Path)
    parser.add_argument("--livekit", choices=("cloud", "selfhosted"), default="selfhosted")
    parser.add_argument("--chains-from-dotenv", action="store_true")
    args = parser.parse_args(argv)

    if not args.dotenv.is_file():
        print(f"render_env: no such file: {args.dotenv}", file=sys.stderr)
        return 1
    if args.inventory:
        print_inventory(inventory(load_dotenv(args.dotenv)))
        return 0
    if args.out is None:
        print("render_env: --out is required to render", file=sys.stderr)
        return 2
    if inside_checkout(args.out):
        print(
            "render_env: refusing to write inside the repository checkout; "
            "pick a directory outside it",
            file=sys.stderr,
        )
        return 1

    report = render(
        load_dotenv(args.dotenv),
        args.out,
        livekit=args.livekit,
        chains_from_dotenv=args.chains_from_dotenv,
    )
    width = max(len(name) for _, name, _ in report)
    for file_name, name, state in report:
        print(f"{file_name:<13} {name:<{width}}  {state}")
    pending = sum(1 for *_, state in report if state.startswith(("owner", "MISSING")))
    print(f"\n{len(report)} settings rendered into {args.out}; {pending} still need the owner.")
    print("Next: copy the files to the box (scp), then run bin/fill_generated.sh there.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
