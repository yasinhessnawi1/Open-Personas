#!/usr/bin/env python
"""Validate the settings every hosted process shares, before anything is written (R9-213).

The api and voice each read their own copy of the six model chains and the OpenRouter
subscription mode (voice composes its own registry, D-V5-6). Those seven settings come from
ONE source, the ``production`` Environment's secrets named exactly like the runtime
settings (:data:`persona_runtime.chain_report.UNIFIED_ENV_NAMES`). The Deploy workflow runs
this script first, in a step that holds the seven values and NOT the Fly token, because it
imports the runtime and its third-party dependencies. It requires every setting present,
every chain a clean model list (the boot line would print it, so not ``withheld`` or
``malformed``) with no retired model, and the mode exactly ``free`` or ``paid``. It writes
nothing. ``scripts/fly_deploy_chains.py`` (standard library only) then stages and verifies.

It never prints a value. GitHub masks a secret only as a complete string, so a fragment of
one (a single chain entry, a line of a pasted ``.env``) would reach the Actions log in
clear. Output is setting names and fixed states; an unexpected failure names its exception
type, never its message. Keep it that way: no ``set -x`` and no ``printenv`` in its step.
"""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

from persona.backends.retired import RETIRED_MODEL_IDS
from persona_runtime.chain_report import model_chains
from persona_runtime.openrouter_subscription import SUBSCRIPTION_MODE_ENV, SUBSCRIPTION_MODES

if TYPE_CHECKING:
    from collections.abc import Mapping

OK = "ok"
MISSING = "missing"
NOTHING_WRITTEN = "Nothing has been written to any app."
_CHAIN_STATES = {
    "withheld": "withheld (an entry is not plainly a model slug)",
    "malformed": "malformed (not a model list)",
}
_INVALID_MODE = "invalid (must be exactly free or paid)"


def check(env: Mapping[str, str]) -> list[tuple[str, str]]:
    """``(name, state)`` for each of the seven settings, in order; ``ok`` or a fixed problem."""
    states: list[tuple[str, str]] = []
    for report in model_chains(env):
        if report.state in ("unset", "empty"):
            states.append((report.name, MISSING))
        elif report.state != "set":
            states.append((report.name, _CHAIN_STATES[report.state]))
        else:
            retired = [i for i, entry in enumerate(report.models, 1) if entry in RETIRED_MODEL_IDS]
            state = f"retired model at position {retired[0]}" if retired else OK
            states.append((report.name, state))
    mode = env.get(SUBSCRIPTION_MODE_ENV)
    if mode is None or not mode.strip():
        states.append((SUBSCRIPTION_MODE_ENV, MISSING))
    else:
        states.append((SUBSCRIPTION_MODE_ENV, OK if mode in SUBSCRIPTION_MODES else _INVALID_MODE))
    return states


def validate(env: Mapping[str, str]) -> int:
    states = check(env)
    for name, state in states:
        print(f"{name}: {state}")
    bad = sum(1 for _, state in states if state != OK)
    if bad:
        print(f"{bad} of {len(states)} settings are not valid. {NOTHING_WRITTEN}")
        return 1
    print(f"All {len(states)} settings are valid. {NOTHING_WRITTEN}")
    return 0


def main(env: Mapping[str, str] | None = None) -> int:
    try:
        return validate(dict(os.environ if env is None else env))
    except Exception as exc:  # noqa: BLE001 - the message could carry a value; name the type
        print(f"validate_deploy_chains failed ({type(exc).__name__}). It prints no values.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
