"""An input file is written inside the execution's input folder, or not at all.

``LocalDockerSandbox._seed_workspace`` wrote each input file at ``host_in / f.path`` without a
check. The paths come from the api (staged documents, skill files), which builds them itself,
but a ``SandboxFile`` path is a plain string and ``../x`` would have been written beside the
execution's folder on the host. It is resolved like any workspace path now, and an escaping
path refuses the execution with a structured error before anything is written.
"""

from __future__ import annotations

from pathlib import Path  # noqa: TC003 - used at runtime in fixtures
from unittest.mock import MagicMock

import pytest
from persona.sandbox.errors import CodeSandboxError
from persona.sandbox.local_docker import LocalDockerSandbox
from persona.sandbox.result import SandboxFile


def _sandbox(tmp_path: Path) -> LocalDockerSandbox:
    return LocalDockerSandbox(workspace_root=tmp_path, docker_client=MagicMock())


@pytest.mark.parametrize(
    "path", ["../escape.txt", "uploads/../../escape.txt", "/escape.txt", "", "."]
)
def test_an_escaping_input_path_is_refused_and_nothing_lands_outside(
    tmp_path: Path, path: str
) -> None:
    host_in = tmp_path / "exec" / "in"
    host_in.mkdir(parents=True)
    with pytest.raises(CodeSandboxError):
        _sandbox(tmp_path)._seed_workspace(
            host_in, "print(1)", [SandboxFile(path=path, content_bytes=b"x", size_bytes=1)]
        )
    assert not (tmp_path / "exec" / "escape.txt").exists()
    assert not (tmp_path / "escape.txt").exists()


def test_a_plain_input_path_is_written_where_it_says(tmp_path: Path) -> None:
    host_in = tmp_path / "exec" / "in"
    host_in.mkdir(parents=True)
    _sandbox(tmp_path)._seed_workspace(
        host_in,
        "print(1)",
        [SandboxFile(path="uploads/report.md", content_bytes=b"# hi", size_bytes=4)],
    )
    assert (host_in / "uploads" / "report.md").read_bytes() == b"# hi"
    assert (host_in / "__persona_main__.py").read_text(encoding="utf-8") == "print(1)"
