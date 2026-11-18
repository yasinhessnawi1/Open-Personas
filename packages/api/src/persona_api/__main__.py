"""Console entrypoint for the persona-api server.

Launch the API cross-platform without a shell script::

    uv run persona-api          # or:  python -m persona_api

It loads the nearest ``.env`` (walking up from the working directory) as-is,
then serves the FastAPI app factory via uvicorn. Defaults are community-friendly
— loopback bind, ``community`` edition (file-based SQLite + Chroma, no auth),
so a fresh checkout runs with zero infrastructure.

This is the portable entrypoint for self-hosters. ``run-local.sh`` is the
owner's local dev harness (cloud edition: Postgres + RLS + Clerk + DeepSeek
tiers + the voice sidecar) and is intentionally separate and unaffected.

Configuration (read from the environment after ``.env`` is loaded):

* ``PERSONA_API_HOST``  — bind host (default ``127.0.0.1``).
* ``PERSONA_API_PORT``  — bind port (default ``8000``).
* ``PERSONA_API_RELOAD``— auto-reload on code change (default off).
"""

from __future__ import annotations

import os
from pathlib import Path

import uvicorn
from persona.logging import get_logger

from persona_api.process_hardening import make_non_dumpable

logger = get_logger("persona_api.main")


def _load_env_file() -> None:
    """Load the nearest ``.env`` (repo root) into the environment, if present.

    Walks up from the working directory and loads the first ``.env`` found.
    Existing environment variables win (``override=False``), so an explicit
    ``PERSONA_API_PORT=9000 uv run persona-api`` still takes effect. A missing
    ``.env`` is fine — the community defaults need no configuration.
    """
    from dotenv import load_dotenv  # ships with uvicorn[standard]

    start = Path.cwd()
    for directory in (start, *start.parents):
        candidate = directory / ".env"
        if candidate.is_file():
            load_dotenv(candidate, override=False)
            logger.info("loaded environment file env_file={}", candidate)
            return


def _flag(name: str, *, default: bool = False) -> bool:
    """Read a boolean-ish environment flag (``1``/``true``/``yes``/``on``)."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def main() -> None:
    """Run the persona-api HTTP server (portable, community-friendly)."""
    # R9-258: harden this process too. Under --reload it is the reloader parent,
    # which holds the env but never runs the app factory.
    make_non_dumpable()
    _load_env_file()

    host = os.environ.get("PERSONA_API_HOST", "127.0.0.1")
    port = int(os.environ.get("PERSONA_API_PORT", "8000"))
    reload = _flag("PERSONA_API_RELOAD")

    logger.info("starting persona-api host={} port={} reload={}", host, port, reload)
    uvicorn.run(
        "persona_api.app:create_app",
        factory=True,
        host=host,
        port=port,
        reload=reload,
    )


if __name__ == "__main__":
    main()
