"""The erase tests' open-file guard still finds a leak where psutil may not list files (R9-284).

CI on a8ea528a: on the GitHub Windows runner ``psutil.Process().open_files()`` raised
``AccessDenied`` at every erase fixture's teardown. The guard now falls back to a rename probe;
these pin that it does, and that the probe finds a real unclosed Chroma client. And the seed
the erase tests copy from a session template (R9-312) is each test's own: what one copy changes
never reaches the template or the next copy.
"""

from __future__ import annotations

import shutil
import sys
from typing import TYPE_CHECKING

import _erase_seed
import psutil
import pytest
from _erase_seed import (
    OWNER,
    PERSONA,
    PERSONA_KINDS,
    files_held_open_under,
    ids_naming,
    legacy_edition,
    open_files_under,
    seed,
)
from persona_api.db.models import personas
from sqlalchemy import delete

if TYPE_CHECKING:
    from pathlib import Path

    from _erase_seed import Seeded, SeedTemplates
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


def _persona_left(seeded: Seeded) -> tuple[list[str], int, bool]:
    memory = sum(
        len(seeded.memory.get_all(persona_id=PERSONA, store_kind=kind)) for kind in PERSONA_KINDS
    )
    folder = (seeded.workspace_root / OWNER / PERSONA / "sandbox" / "out.txt").exists()
    return ids_naming(seeded.engine, "personas", PERSONA), memory, folder


def test_a_copied_seed_is_the_tests_own_and_the_template_never_changes(
    tmp_path: Path, embedder: Embedder, erase_templates: SeedTemplates
) -> None:
    """R9-312: a copy changed in every store leaves the template, and the next copy, whole."""
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    with legacy_edition(first, embedder, erase_templates) as one:
        template = erase_templates.fingerprint()
        assert _persona_left(one) == ([PERSONA], len(PERSONA_KINDS), True)
        with one.engine.begin() as conn:
            conn.execute(delete(personas).where(personas.c.id == PERSONA))
        for kind in PERSONA_KINDS:
            one.memory.delete_persona(PERSONA, kind)
        shutil.rmtree(one.workspace_root / OWNER / PERSONA)
        assert _persona_left(one) == ([], 0, False)

    with legacy_edition(second, embedder, erase_templates) as two:
        assert _persona_left(two) == ([PERSONA], len(PERSONA_KINDS), True)

    assert erase_templates.fingerprint() == template
    assert template, "the copies came from a template"
    assert all(state.endswith(" read-only") for state in template.values()), template
