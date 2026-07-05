"""Local dev entrypoint for persona-voice — loads the repo-root ``.env`` (the single
source of truth) then serves the real app. Run it with a normal uvicorn command:

    cd packages/voice && uv run uvicorn persona_voice.dev:create_app --factory \
        --host 127.0.0.1 --port 8001

Any ``PERSONA_*`` var in ``.env`` is picked up on restart — no launcher to edit.
The test suites import ``persona_voice.http.app`` directly and never load ``.env``.
"""

from __future__ import annotations

from persona.local_env import load_local_env

load_local_env()  # MUST run before the app import so import-time reads see .env

from persona_voice.http.app import create_app  # noqa: E402 — intentional: env first

__all__ = ["create_app"]
