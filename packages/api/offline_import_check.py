#!/usr/bin/env python
"""Prove the api's import graph needs no network: the api, its connectors, release.py and
every migration import with every socket refused.

Why: on the Oracle host the one-shot `migrate` container sits only on the internal `db`
network, with no route out, by design. A migration imports persona.tasks.checkpoint ->
persona.skills._tokens, which builds tiktoken's cl100k_base encoder at import, and tiktoken
downloads the encoding file the first time. With no network the migration died at import,
and the api would have re-downloaded it on every cold start. The image now bakes the file
(packages/api/Dockerfile, TIKTOKEN_CACHE_DIR); this check keeps it that way:

    python packages/api/offline_import_check.py            # in the image: real tiktoken
    python packages/api/offline_import_check.py --stub-tiktoken   # anywhere else

It refuses every connection attempt and name lookup, imports the graph, loads every
migration module through alembic, then (without --stub-tiktoken) builds every encoding
the graph asked for, for real. It prints one JSON line and exits 1 if anything tried the
network or an encoding the image does not bake was requested.
"""

from __future__ import annotations

import importlib.util
import json
import socket
import sys
from pathlib import Path
from typing import Any

#: The encodings the api image bakes into TIKTOKEN_CACHE_DIR (packages/api/Dockerfile).
BAKED_ENCODINGS = frozenset({"cl100k_base"})
API_ROOT = Path(__file__).resolve().parent

_attempts: list[str] = []


def _deny(*args: Any, **_kwargs: Any) -> Any:  # noqa: ANN401 - mirrors socket's signatures
    _attempts.append(repr(args[1:3] if len(args) > 1 else args))
    raise OSError("network refused by offline_import_check.py")


def _refuse_the_network() -> None:
    socket.socket.connect = _deny  # type: ignore[method-assign]
    socket.socket.connect_ex = _deny  # type: ignore[method-assign]
    socket.create_connection = _deny
    socket.getaddrinfo = _deny


def _record_encodings(stub: bool) -> list[str]:
    import tiktoken

    requested: list[str] = []
    real = tiktoken.get_encoding

    class _Stub:
        def encode(self, text: str, **_kwargs: Any) -> list[int]:  # noqa: ANN401
            return list(text.encode())

    def get_encoding(name: str) -> Any:  # noqa: ANN401
        requested.append(name)
        return _Stub() if stub else real(name)

    tiktoken.get_encoding = get_encoding  # type: ignore[assignment]
    return requested


def _import_the_graph() -> int:
    import persona_api.app  # noqa: F401
    import persona_connectors.service  # noqa: F401

    spec = importlib.util.spec_from_file_location("release_offline", API_ROOT / "release.py")
    assert spec is not None
    assert spec.loader is not None
    spec.loader.exec_module(importlib.util.module_from_spec(spec))

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config()
    config.set_main_option("script_location", str(API_ROOT / "alembic"))
    return len(list(ScriptDirectory.from_config(config).walk_revisions()))


def main(argv: list[str]) -> int:
    stub = "--stub-tiktoken" in argv
    _refuse_the_network()
    requested = _record_encodings(stub)
    migrations = _import_the_graph()
    unbaked = sorted(set(requested) - BAKED_ENCODINGS)
    print(
        json.dumps(
            {
                "network_attempts": _attempts,
                "encodings": sorted(set(requested)),
                "unbaked_encodings": unbaked,
                "migrations": migrations,
                "tiktoken": "stub" if stub else "real",
            }
        )
    )
    return 1 if _attempts or unbaked else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
