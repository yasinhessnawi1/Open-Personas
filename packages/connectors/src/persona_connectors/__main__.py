"""The connector service entry point (Spec C2 T9 / C3) -- ``python -m persona_connectors``.

A thin launcher. Every composition decision lives in :mod:`persona_connectors.service`,
which the api can also import to host the same runners in-process (Spec I1, D-I1-9: library
code must never import a module named ``__main__``, so the builder moved out and this file
kept only the process entry).
"""

from __future__ import annotations

import asyncio

from persona.logging import get_logger

from persona_connectors.service import run_standalone

_log = get_logger("connectors.service")


def main() -> None:
    """Run the connector service (the ``python -m persona_connectors`` entry).

    Fly sends SIGINT on deploy (``kill_signal = "SIGINT"``). The interpreter raises
    ``KeyboardInterrupt`` on the main thread, and because it surfaces inside a Task it
    goes to the EVENT LOOP rather than to the awaiting coroutine, so it comes back out of
    ``asyncio.run`` chained to the runners' ``CancelledError``. The process was exiting on
    a traceback for an ordinary, successful stop, which is the kind of log noise that
    teaches an operator to ignore tracebacks. Catching it HERE is the only place that
    works; ``run_standalone``'s own teardown has already run by then.
    """
    try:
        asyncio.run(run_standalone())
    except KeyboardInterrupt:
        _log.info("connector service stopped by signal")


if __name__ == "__main__":
    main()
