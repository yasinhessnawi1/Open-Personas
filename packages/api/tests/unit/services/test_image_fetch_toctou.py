"""Service-level TOCTOU test for the image download path (Spec R2, T3 / F-03).

``image_service.fetch`` is the most-exposed serve site (user-facing download,
user-controlled ``ref``). This proves the end-to-end hardening: a regular image
that is swapped for a symlink pointing OUTSIDE the sandbox after resolution is
served as ``not_found`` (via the ``is_regular_file_nofollow`` lstat guard +
``read_nofollow_bytes``), never leaking the out-of-sandbox bytes.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from persona.errors import PersonaError
from persona_api.services import image_service
from persona_api.storage import LocalFileStorage


def test_fetch_refuses_a_symlink_swapped_for_an_outside_file(tmp_path: Path) -> None:
    workspace_root = tmp_path / "ws"
    owner_id, persona_id = "owner1", "persona1"
    uploads = workspace_root / owner_id / persona_id / "uploads"
    uploads.mkdir(parents=True)

    # A secret OUTSIDE every sandbox root.
    outside = tmp_path / "outside_secret.png"
    outside.write_bytes(b"\x89PNG\r\n\x1a\n-OUTSIDE-SECRET")

    # The swap: the served ref's final component is now a symlink to the secret.
    victim = uploads / "avatar.png"
    victim.symlink_to(outside)

    with pytest.raises(PersonaError) as exc_info:
        image_service.fetch(
            file_storage=LocalFileStorage(workspace_root),
            owner_id=owner_id,
            persona_id=persona_id,
            ref="avatar.png",
        )
    assert exc_info.value.context.get("reason") == "not_found"


def test_fetch_serves_a_genuine_regular_file(tmp_path: Path) -> None:
    """Regression floor: a real in-sandbox image still serves byte-for-byte."""
    workspace_root = tmp_path / "ws"
    owner_id, persona_id = "owner1", "persona1"
    uploads = workspace_root / owner_id / persona_id / "uploads"
    uploads.mkdir(parents=True)
    payload = b"\x89PNG\r\n\x1a\n-real-bytes"
    (uploads / "real.png").write_bytes(payload)

    data, media_type = image_service.fetch(
        file_storage=LocalFileStorage(workspace_root),
        owner_id=owner_id,
        persona_id=persona_id,
        ref="real.png",
    )
    assert data == payload
    assert media_type == "image/png"
