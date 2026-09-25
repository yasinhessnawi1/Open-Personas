#!/usr/bin/env python3
"""Stage and verify, on Fly, the settings the api and voice share (R9-213 part 2).

The api and voice each read their own copy of the six model chains and the OpenRouter
subscription mode, so the Deploy workflow hands both apps the same seven values from one
source (the ``production`` Environment's secrets). ``scripts/validate_deploy_chains.py``
checks them first, with the runtime's own parser and without the Fly token. This script
then does the two things that need the token:

* ``stage APP...``: re-checks every value against the strict model slug charset (the mode
  against exactly ``free`` or ``paid``), then feeds the same seven ``NAME=VALUE`` lines to
  ``flyctl secrets import --stage -a APP`` on stdin, for every app, stopping at the first
  failure. Each app applies them on its next deploy.
* ``verify APP...``: after the deploys, reads ``flyctl secrets list --json`` for each app
  and reports, per setting and app, ``match``, ``differs``, ``missing`` or ``not
  deployed``. It compares full digests and never prints a character of one: the Actions
  log of this repository is public.

It holds the Fly API token, so it imports the Python standard library ONLY and the
workflow runs it with ``python3 -I`` (isolated: no PYTHONPATH, no user site packages, no
script directory on ``sys.path``). No third-party code ever runs in a process that has the
token. flyctl gets the token in its own process environment, next to a short allow-list of
what it needs to run, and never the seven values, which reach it on stdin.

It never prints a value: output is setting names, app names and fixed words. flyctl's
stdout and stderr are discarded whenever values went in; for ``secrets list`` stdout (the
JSON of names, digests and statuses) is parsed and stderr (warnings) is discarded, never
merged into the parse. An unexpected failure names its exception type, never its message.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

#: The seven shared settings: a second copy of
#: ``persona_runtime.chain_report.UNIFIED_ENV_NAMES``, kept here because this script must
#: not import the runtime. ``scripts/tests/test_fly_deploy_chains.py`` fails if they differ.
SHARED_NAMES = (
    "PERSONA_FRONTIER_MODELS",
    "PERSONA_MID_MODELS",
    "PERSONA_SMALL_MODELS",
    "PERSONA_FREE_FRONTIER_MODELS",
    "PERSONA_FREE_MID_MODELS",
    "PERSONA_FREE_SMALL_MODELS",
    "PERSONA_OPENROUTER_SUBSCRIPTION_MODE",
)
MODE_NAME = "PERSONA_OPENROUTER_SUBSCRIPTION_MODE"
MODES = ("free", "paid")
TOKEN_NAME = "FLY_API_TOKEN"
NOTHING_WRITTEN = "Nothing has been written to any app."

#: The strict slug charset, the same shape the boot line prints: ``provider/model`` in
#: letters, digits and ``._:/-``, the Workers AI ``@cf/`` or ``@hf/`` namespace the only ``@``.
_ENTRY = re.compile(r"[A-Za-z0-9._-]+/(?:@(?:cf|hf)/)?[A-Za-z0-9._:/-]+")

#: What flyctl may inherit besides the token: enough to find itself, its config, temp space,
#: certificates and a proxy. Never the seven values.
_FLYCTL_ENV = (
    "PATH",
    "HOME",
    "TMPDIR",
    "TEMP",
    "TMP",
    "LANG",
    "LC_ALL",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
    "SYSTEMROOT",
)


def canonical(name: str, raw: str | None) -> str | None:
    """The value to stage for ``name``, or ``None`` when it fails the re-check."""
    if raw is None:
        return None
    if name == MODE_NAME:
        return raw if raw in MODES else None
    entries = []
    for part in raw.split(","):
        provider, slash, model = part.strip().partition("/")
        entry = f"{provider.strip()}/{model.strip()}"
        if not slash or _ENTRY.fullmatch(entry) is None or "://" in entry:
            return None
        entries.append(entry)
    return ",".join(entries)


def _flyctl_env(env: dict[str, str], token: str) -> dict[str, str]:
    child = {key: env[key] for key in _FLYCTL_ENV if key in env}
    child[TOKEN_NAME] = token
    return child


def stage(apps: list[str], env: dict[str, str], flyctl: tuple[str, ...] = ("flyctl",)) -> int:
    token = env.get(TOKEN_NAME, "")
    if not token.strip():
        print(f"Refusing to stage: {TOKEN_NAME} is not set. {NOTHING_WRITTEN}")
        return 1
    values = {name: canonical(name, env.get(name)) for name in SHARED_NAMES}
    failed = [name for name, value in values.items() if value is None]
    if failed:
        print(f"Refusing to stage: {', '.join(failed)} failed the re-check. {NOTHING_WRITTEN}")
        return 1
    payload = "".join(f"{name}={values[name]}\n" for name in SHARED_NAMES)
    child_env = _flyctl_env(env, token)
    for app in apps:
        completed = subprocess.run(  # a fixed argv, never a shell
            [*flyctl, "secrets", "import", "--stage", "-a", app],
            input=payload,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=child_env,
            check=False,
        )
        if completed.returncode != 0:
            print(
                f"flyctl secrets import failed on {app} (exit {completed.returncode}). Its "
                "output is withheld because it can carry values. Check that FLY_API_TOKEN "
                "may write secrets on that app."
            )
            return 1
        print(f"Staged {len(SHARED_NAMES)} settings on {app}.")
    print("Each app applies them on its next deploy.")
    return 0


def _listing(
    app: str, env: dict[str, str], flyctl: tuple[str, ...]
) -> dict[str, tuple[str, str]] | str:
    """``{name: (digest, status)}`` for one app, or the line to print when that fails."""
    completed = subprocess.run(  # a fixed argv, never a shell
        [*flyctl, "secrets", "list", "-a", app, "--json"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
        check=False,
    )
    if completed.returncode != 0:
        return f"flyctl secrets list failed on {app} (exit {completed.returncode})."
    try:
        rows = json.loads(completed.stdout)
        return {str(row["name"]): (str(row["digest"]), str(row["status"])) for row in rows}
    except (ValueError, KeyError, TypeError):
        return f"flyctl secrets list on {app} did not answer with the expected JSON."


def _state(app: str, seen: dict[str, tuple[str, str] | None]) -> str:
    """One app's word for one setting. Full digests are compared; none is ever returned."""
    row = seen[app]
    if row is None:
        return "missing"
    if row[1] != "Deployed":
        return "not deployed"
    others = [other for name, other in seen.items() if name != app]
    if others and all(other is not None and other[0] == row[0] for other in others):
        return "match"
    return "differs"


def verify(apps: list[str], env: dict[str, str], flyctl: tuple[str, ...] = ("flyctl",)) -> int:
    token = env.get(TOKEN_NAME, "")
    if not token.strip():
        print(f"Cannot verify: {TOKEN_NAME} is not set.")
        return 1
    child_env = _flyctl_env(env, token)
    listings: dict[str, dict[str, tuple[str, str]]] = {}
    for app in apps:
        listing = _listing(app, child_env, flyctl)
        if isinstance(listing, str):
            print(listing)
            return 1
        listings[app] = listing
    bad = 0
    for name in SHARED_NAMES:
        seen = {app: listings[app].get(name) for app in apps}
        states = {app: _state(app, seen) for app in apps}
        print(f"{name}: " + ", ".join(f"{app} {state}" for app, state in states.items()))
        bad += 0 if set(states.values()) == {"match"} else 1
    total = len(SHARED_NAMES)
    if bad:
        print(f"{bad} of {total} settings do not match or are not deployed on {len(apps)} apps.")
        return 1
    print(f"All {total} settings match and are deployed on {len(apps)} apps.")
    return 0


def main(argv: list[str] | None = None, env: dict[str, str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 2 or args[0] not in ("stage", "verify"):
        print("usage: fly_deploy_chains.py {stage|verify} APP [APP ...]")
        return 2
    command, apps = args[0], args[1:]
    environment = dict(os.environ if env is None else env)
    if env is None:
        # The token lives on only in flyctl's own process environment from here on.
        os.environ.pop(TOKEN_NAME, None)
    try:
        if command == "stage":
            return stage(apps, environment)
        return verify(apps, environment)
    except Exception as exc:  # noqa: BLE001 - the message could carry a value; name the type
        print(f"fly_deploy_chains {command} failed ({type(exc).__name__}). It prints no values.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
