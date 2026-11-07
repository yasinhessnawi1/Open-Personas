"""Path parameters shared by the routers, declared once.

``PersonaIdPath`` is the only way a route takes ``{persona_id}``. FastAPI checks it before the
handler or any of its work runs, so an id such as ``%2E%2E`` (which arrives decoded as ``..``)
is a 422 and never reaches a lookup, a storage key or a folder. A unit test walks the
application's routes and fails for any ``{persona_id}`` route declared without it.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Path

from persona_api.workspace_paths import PERSONA_ID_PATTERN

__all__ = ["PersonaIdPath"]

PersonaIdPath = Annotated[
    str,
    Path(
        pattern=PERSONA_ID_PATTERN,
        description="A persona id: persona_ followed by 32 lower-case hex digits.",
    ),
]
