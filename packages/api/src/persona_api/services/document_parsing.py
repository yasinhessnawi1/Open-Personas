"""The port every untrusted document parse goes through (D-FV0-31).

The app wires :class:`persona_api.document_worker.pool.ProcessDocumentParser` as
``app.state.document_parser``: the parse runs in a limited, killable worker process.
Callers (the upload route, and the re-parse of an older whole-inject document) only see
:class:`DocumentParser`.

:class:`ThreadDocumentParser` runs the same function in this process. It exists for
tests that replace the parse function in-process; the app never wires it.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from persona_api.document_worker.admission import UploadLease
from persona_api.document_worker.pool import ParsePurpose
from persona_api.document_worker.untrusted import (
    ParsedDocument,
    parse_untrusted_document,
    resolve_raster_dpi,
)

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "DocumentParser",
    "ParsePurpose",
    "ParsedDocument",
    "ThreadDocumentParser",
]


@runtime_checkable
class DocumentParser(Protocol):
    """Parses an untrusted document off the event loop."""

    async def parse(
        self,
        path: Path,
        *,
        render_dir: Path | None,
        owner_id: str,
        purpose: ParsePurpose,
        lease: UploadLease | None = None,
    ) -> ParsedDocument:
        """Parse ``path`` (see ``untrusted.parse_untrusted_document``).

        ``owner_id``, ``purpose`` and ``lease`` drive admission: one upload per owner at
        a time (the lease covers the store too), each owner's worker-time budget, and
        how long a parse may wait for a busy parser.
        """
        ...


class ThreadDocumentParser:
    """Runs the parse in a thread of this process. For tests only; never wired in the app."""

    async def parse(
        self,
        path: Path,
        *,
        render_dir: Path | None,
        owner_id: str,  # noqa: ARG002 (no admission in-process)
        purpose: ParsePurpose,  # noqa: ARG002
        lease: UploadLease | None = None,  # noqa: ARG002
    ) -> ParsedDocument:
        """Parse ``path`` in a worker thread."""
        return await asyncio.to_thread(
            parse_untrusted_document, path, render_dir=render_dir, dpi=resolve_raster_dpi()
        )
