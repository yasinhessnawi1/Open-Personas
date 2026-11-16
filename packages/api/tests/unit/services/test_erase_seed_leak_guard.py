"""The erase tests' open-file guard still finds a leak where psutil may not list files (R9-284).

CI on a8ea528a: on the GitHub Windows runner ``psutil.Process().open_files()`` raised
``AccessDenied`` at every erase fixture's teardown. The guard now falls back to a rename probe;
these pin that it does, and that the probe finds a real unclosed Chroma client.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import _erase_seed
import psutil
import pytest
from _erase_seed import files_held_open_under, open_files_under, seed

if TYPE_CHECKING:
    from pathlib import Path

    from persona.stores.embedder import Embedder


def test_a_refused_listing_falls_back_to_the_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Refused:
        def open_files(self) -> list[object]:
            raise psutil.AccessDenied(pid=5832)

    monkeypatch.setattr(_erase_seed.psutil, "Process", _Refused)
    monkeypatch.setattr(_erase_seed, "files_held_open_under", lambda root: [f"probed {root.name}"])

    assert open_files_under(tmp_path) == [f"probed {tmp_path.name}"]


@pytest.mark.skipif(sys.platform != "win32", reason="the probe relies on Windows file sharing")
def test_the_probe_finds_an_unclosed_chroma_client_and_not_a_closed_one(
    tmp_path: Path, embedder: Embedder
) -> None:
    seeded = seed(tmp_path, embedder)
    try:
        held = files_held_open_under(tmp_path)
        assert any("chroma" in path for path in held), held
    finally:
        seeded.memory.close()  # type: ignore[attr-defined]
        seeded.engine.dispose()

    assert files_held_open_under(tmp_path) == []
    assert not list(tmp_path.rglob("*.leakprobe")), "every probe renamed back"
