"""An explicit, platform-independent table of file extensions to media types (Spec WIN, T2).

Python's :mod:`mimetypes` answers differently on every machine: on Windows it reads the
registry (with Office installed, ``.csv`` becomes ``application/vnd.ms-excel``), on
Linux it reads ``/etc/mime.types`` when the ``media-types`` package is installed (the
slim production image has none, so ``.docx`` there was ``application/octet-stream``),
and its built-in table even changes between Python patch releases. The media type
decides how a file renders (an ``image/*`` renders inline, ``text/csv`` as a table), so
it must not depend on where the product runs.

The values are the ones Ubuntu's ``/etc/mime.types`` gives, which is what development
and CI machines produced and what the tests assert; an extension outside the table is
``application/octet-stream`` everywhere.
"""

from __future__ import annotations

from pathlib import PurePosixPath

__all__ = ["DEFAULT_MEDIA_TYPE", "extension_for_media_type", "media_type_for"]

#: The media type of anything the table does not name.
DEFAULT_MEDIA_TYPE = "application/octet-stream"

_MEDIA_TYPES: dict[str, str] = {
    # images
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".ico": "image/vnd.microsoft.icon",
    # documents
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".doc": "application/msword",
    ".xls": "application/vnd.ms-excel",
    ".ppt": "application/vnd.ms-powerpoint",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".ods": "application/vnd.oasis.opendocument.spreadsheet",
    ".odp": "application/vnd.oasis.opendocument.presentation",
    ".rtf": "application/rtf",
    ".epub": "application/epub+zip",
    # text and data
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".html": "text/html",
    ".htm": "text/html",
    ".css": "text/css",
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".xml": "application/xml",
    ".json": "application/json",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".py": "text/x-python",
    ".sql": "application/sql",
    # archives
    ".zip": "application/zip",
    ".tar": "application/x-tar",
    # audio and video
    ".mp3": "audio/mpeg",
    ".wav": "audio/x-wav",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
    ".flac": "audio/flac",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
}

#: The preferred extension for a media type: the first extension listed for it above.
_EXTENSIONS: dict[str, str] = {
    media_type: extension for extension, media_type in reversed(list(_MEDIA_TYPES.items()))
}


def media_type_for(path: str) -> str:
    """The media type for ``path``'s extension (case-insensitive), or the default.

    Only the extension is consulted; no I/O is performed, and the answer is the same on
    every platform and machine.
    """
    return _MEDIA_TYPES.get(
        PurePosixPath(path.replace("\\", "/")).suffix.lower(), DEFAULT_MEDIA_TYPE
    )


def extension_for_media_type(media_type: str) -> str | None:
    """The preferred extension (with the dot) for ``media_type``, or ``None`` if unknown."""
    return _EXTENSIONS.get(media_type.split(";", 1)[0].strip().lower())
