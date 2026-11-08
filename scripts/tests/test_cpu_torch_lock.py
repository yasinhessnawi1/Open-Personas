"""Linux installs from the lock take the CPU build of PyTorch, never the CUDA one.

Production never had a GPU. PyPI's Linux torch wheels pull about 2.3 GB of compressed
CUDA wheels (nvidia-*, cuda-*, triton) on x86_64 AND aarch64, which made the api image
7 to 8 GB and could not be built on the 45 GB Oracle box (ruling D1, 2026-11-07). The
lock therefore takes torch for Linux from PyTorch's CPU index, declared explicit so that
index serves torch and nothing else.

What is pinned here:

* the CPU index is declared once, explicit, at the official URL;
* torch is a direct dependency of persona-core (uv honours a source only for a direct
  dependency, and sentence-transformers needs torch anyway), sourced from that index on
  Linux only, so Windows and macOS keep PyPI's wheels;
* the lock resolves torch for Linux from that index for both architectures the deploy
  needs, and holds no CUDA package at all.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
CPU_INDEX_URL = "https://download.pytorch.org/whl/cpu"
INDEX_NAME = "pytorch-cpu"
LINUX = "sys_platform == 'linux'"
CUDA_PACKAGE = re.compile(r"^(nvidia-|cuda-|triton$)")


def _toml(path: Path) -> dict[str, Any]:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _lock() -> dict[str, Any]:
    return _toml(ROOT / "uv.lock")


def cuda_packages(lock: dict[str, Any]) -> list[str]:
    """Every locked package that is part of a CUDA build of torch."""
    return sorted(p["name"] for p in lock["package"] if CUDA_PACKAGE.match(p["name"]))


def torch_entries(lock: dict[str, Any]) -> list[dict[str, Any]]:
    return [p for p in lock["package"] if p["name"] == "torch"]


def linux_cpu_wheels(lock: dict[str, Any]) -> list[str]:
    """The cp312 Linux wheel file names of every torch entry locked from the CPU index."""
    names = []
    for entry in torch_entries(lock):
        if entry.get("source", {}).get("registry") != CPU_INDEX_URL:
            continue
        for wheel in entry.get("wheels", []):
            name = wheel["url"].rsplit("/", 1)[-1].replace("%2B", "+")
            if "cp312" in name and "linux" in name:
                names.append(name)
    return names


def test_the_cpu_index_is_declared_once_and_explicit() -> None:
    indexes = _toml(ROOT / "pyproject.toml").get("tool", {}).get("uv", {}).get("index", [])
    matching = [i for i in indexes if i.get("name") == INDEX_NAME]
    assert len(matching) == 1, f"declare exactly one [[tool.uv.index]] named {INDEX_NAME}"
    assert matching[0]["url"] == CPU_INDEX_URL
    # explicit: the index serves only packages pinned to it, so numpy, pillow and the
    # rest still come from PyPI.
    assert matching[0].get("explicit") is True


def test_torch_is_a_direct_core_dependency_sourced_from_the_cpu_index_on_linux() -> None:
    core = _toml(ROOT / "packages" / "core" / "pyproject.toml")
    direct = [d for d in core["project"]["dependencies"] if re.match(r"^torch\b", d)]
    assert direct, "torch must be a direct persona-core dependency for its source to apply"
    root_sources = _toml(ROOT / "pyproject.toml")["tool"]["uv"]["sources"]
    assert root_sources.get("torch") == [{"index": INDEX_NAME, "marker": LINUX}]


def test_the_lock_takes_linux_torch_from_the_cpu_index_for_both_architectures() -> None:
    wheels = linux_cpu_wheels(_lock())
    assert any(w.endswith("manylinux_2_28_aarch64.whl") for w in wheels), wheels
    assert any(w.endswith("manylinux_2_28_x86_64.whl") for w in wheels), wheels
    assert all("+cpu" in w for w in wheels), wheels


_NOT_LINUX = re.compile(r"sys_platform (?:!= 'linux'|== '(?!linux')[a-z0-9]+')")


def pypi_torch_admits_linux(lock: dict[str, Any]) -> bool:
    """True when a torch entry NOT from the CPU index can still resolve on Linux.

    An entry without resolution markers resolves everywhere; with markers, every one of
    them must rule Linux out (``!= 'linux'`` or another named platform).
    """
    for entry in torch_entries(lock):
        if entry.get("source", {}).get("registry") == CPU_INDEX_URL:
            continue
        markers = entry.get("resolution-markers", [])
        if not markers or not all(_NOT_LINUX.search(m) for m in markers):
            return True
    return False


def test_no_torch_entry_for_linux_comes_from_pypi() -> None:
    assert not pypi_torch_admits_linux(_lock())


def test_the_linux_check_is_not_vacuous() -> None:
    """The old lock's single unmarked PyPI entry is exactly what the check must catch."""
    unmarked = {"package": [{"name": "torch", "version": "2.12.0", "source": {"registry": "x"}}]}
    assert pypi_torch_admits_linux(unmarked)
    linux_marked = {
        "package": [
            {
                "name": "torch",
                "version": "2.12.0",
                "source": {"registry": "x"},
                "resolution-markers": ["sys_platform == 'win32'", "sys_platform == 'linux'"],
            }
        ]
    }
    assert pypi_torch_admits_linux(linux_marked)


def test_the_lock_holds_no_cuda_package() -> None:
    assert cuda_packages(_lock()) == []


# Every image build pins uv by version AND digest, at the uv that wrote this lock, so a
# re-tag upstream cannot change what builds production. (The T1 note said the floating
# `uv:0.6` could not read lock revision 3; the review found 0.6.9 reads it. The pin stays
# for reproducibility, not for that reason.)
UV_IMAGE = re.compile(
    r"^COPY --from=ghcr\.io/astral-sh/uv:(\d+)\.(\d+)\.(\d+)@sha256:[0-9a-f]{64}"
    r" /uv /usr/local/bin/uv$",
    re.M,
)
DOCKERFILES = sorted((ROOT / "packages").glob("*/Dockerfile"))


def test_every_dockerfile_pins_a_uv_that_reads_this_lock() -> None:
    assert DOCKERFILES
    for dockerfile in DOCKERFILES:
        text = dockerfile.read_text(encoding="utf-8")
        if "astral-sh/uv" not in text:
            continue
        match = UV_IMAGE.search(text)
        assert match, f"{dockerfile.parent.name}/Dockerfile must pin uv by version and digest"
        assert tuple(int(g) for g in match.groups()) >= (0, 12, 0), match.group(0)


def _core_entry(lock: dict[str, Any]) -> dict[str, Any]:
    return next(p for p in lock["package"] if p["name"] == "persona-core")


def test_the_lock_records_torch_as_a_direct_core_dependency() -> None:
    """Review of T1: the lock kept torch under the `local` extra only, so `uv lock --check`
    failed. persona-core's own edges and its recorded requires-dist must both carry it."""
    core = _core_entry(_lock())
    edges = [d for d in core.get("dependencies", []) if d["name"] == "torch"]
    assert edges, "persona-core has no torch edge in uv.lock"
    assert {"sys_platform == 'linux'", "sys_platform != 'linux'"} <= {
        d.get("marker", "") for d in edges
    }
    required = [d for d in core["metadata"]["requires-dist"] if d["name"] == "torch"]
    unconditional = [d for d in required if "extra ==" not in d.get("marker", "")]
    linux = [d for d in unconditional if d.get("marker") == LINUX]
    assert linux, "the lock does not record torch as a base dependency on Linux"
    assert linux[0].get("index") == CPU_INDEX_URL


def test_ci_fails_on_a_stale_lock_before_it_syncs() -> None:
    """Review of T1: a stale lock passed every check. CI's first job now refuses one."""
    import yaml

    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    runs = [str(s.get("run", "")) for s in ci["jobs"]["lint-and-type-check"]["steps"]]
    assert "uv lock --check" in runs, "the lint job does not check the lock"
    first_sync = next(i for i, r in enumerate(runs) if r.startswith("uv sync"))
    assert runs.index("uv lock --check") < first_sync
