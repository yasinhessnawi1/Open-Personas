"""The api image installs only what the api process runs, and builds on arm64.

The Oracle server is aarch64 (2026-11-07). The api image used ``uv sync --all-packages``,
which also installed persona-voice and with it ``silero-vad-lite``, a package with no
Linux aarch64 wheel (``uv.lock``: sdist only). The api never imports persona_voice; it
reaches voice over HTTP. ``persona-connectors`` depends on ``persona-api`` (C1-D-1), and
the api hosts the connector transports in process (the I1 fold), so syncing that one
package installs exactly the api's closure: connectors, api, runtime and core[postgres].

Pinned here: the sync line, that its dependency closure in the lock holds no voice
package and has an aarch64 wheel (or is pure Python) for every native package, the uv
cache mount that keeps the cache out of the image layer, and that no ``.env`` file
anywhere in the build context reaches an image.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "packages" / "api" / "Dockerfile"
SYNC_PACKAGE = "persona-connectors"
VOICE_ONLY = {"persona-voice", "silero-vad-lite", "livekit", "livekit-api", "livekit-agents"}
WINDOWS_ONLY = re.compile(r"sys_platform == 'win32'|platform_system == 'Windows'")


def _lock() -> dict[str, Any]:
    return tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))


def _sync_lines() -> list[str]:
    text = DOCKERFILE.read_text(encoding="utf-8").replace("\\\n", " ")
    return [line for line in text.splitlines() if re.search(r"\buv sync\b", line)]


def closure(lock: dict[str, Any], root: str) -> set[str]:
    """Every package ``uv sync --package root`` can install, markers ignored except Windows.

    Extras named on a dependency (``persona-core[postgres]``) follow that package's
    optional dependencies for those extras. Ignoring markers over-approximates, which is
    the safe direction for a "nothing voice, nothing x86-only" check.
    """
    by_name = {p["name"]: p for p in lock["package"]}
    seen: set[str] = set()
    todo: list[tuple[str, tuple[str, ...]]] = [(root, ())]
    while todo:
        name, extras = todo.pop()
        key = f"{name}[{','.join(sorted(extras))}]"
        if key in seen:
            continue
        seen.add(key)
        package = by_name[name]
        deps = list(package.get("dependencies", []))
        for extra in extras:
            deps.extend(package.get("optional-dependencies", {}).get(extra, []))
        for dep in deps:
            if WINDOWS_ONLY.search(dep.get("marker", "")):
                continue
            todo.append((dep["name"], tuple(dep.get("extra", []))))
    return {k.split("[", 1)[0] for k in seen}


def lacks_aarch64_wheel(package: dict[str, Any]) -> bool:
    """A registry package with wheels, none of them pure Python or cp312 Linux aarch64."""
    source = package.get("source", {})
    if "registry" not in source:
        return False  # a workspace member: built from source, pure Python
    wheels = [w["url"].rsplit("/", 1)[-1] for w in package.get("wheels", [])]
    if any(w.endswith("-none-any.whl") for w in wheels):
        return False
    return not any(
        "aarch64" in w and "musllinux" not in w and re.search(r"-(cp312|cp3\d+-abi3|py3-none)", w)
        for w in wheels
    )


def test_the_api_image_syncs_only_the_api_closure() -> None:
    lines = _sync_lines()
    assert len(lines) == 1, lines
    assert "--all-packages" not in lines[0]
    for flag in ("--frozen", "--no-dev", f"--package {SYNC_PACKAGE}"):
        assert flag in lines[0], f"the api image's uv sync lacks {flag}"


def test_the_uv_cache_stays_out_of_the_image_layer() -> None:
    assert "--mount=type=cache,target=/root/.cache/uv" in _sync_lines()[0]


def test_connectors_bring_the_api_with_them() -> None:
    deps = (ROOT / "packages" / "connectors" / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r'^\s*"persona-api', deps, re.M), "connectors must depend on persona-api"


def test_the_closure_holds_no_voice_package() -> None:
    installed = closure(_lock(), SYNC_PACKAGE)
    assert {"persona-api", "persona-core", "persona-runtime", "torch"} <= installed
    assert not installed & VOICE_ONLY, sorted(installed & VOICE_ONLY)


def test_every_native_package_in_the_closure_has_an_arm64_wheel() -> None:
    lock = _lock()
    by_name = {p["name"]: p for p in lock["package"]}
    missing = []
    for name in sorted(closure(lock, SYNC_PACKAGE)):
        # torch has two entries (PyPI for non-Linux, the CPU index for Linux).
        entries = [p for p in lock["package"] if p["name"] == name] or [by_name[name]]
        if all(lacks_aarch64_wheel(p) for p in entries):
            missing.append(name)
    assert missing == []


def test_the_checks_are_not_vacuous() -> None:
    """The whole workspace DOES reach silero-vad-lite, which DOES lack an arm64 wheel."""
    lock = _lock()
    assert "silero-vad-lite" in closure(lock, "persona-voice")
    silero = next(p for p in lock["package"] if p["name"] == "silero-vad-lite")
    assert lacks_aarch64_wheel(silero)


def test_no_env_file_anywhere_reaches_a_build() -> None:
    patterns = {
        line.strip()
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert {"**/.env", "**/.env.*"} <= patterns


#: Review of T1 to T6 (LOW): the Python base is pinned by its multi-arch index digest in
#: every Dockerfile, so amd64 and arm64 build from the same release and a re-tag upstream
#: cannot change a production build. Bump the digest deliberately, for security updates.
BASE = re.compile(r"^FROM python:3\.12-slim@sha256:[0-9a-f]{64} AS (builder|runtime)$")


def test_every_python_base_is_pinned_by_digest() -> None:
    checked = set()
    for dockerfile in sorted((ROOT / "packages").glob("*/Dockerfile")):
        for line in dockerfile.read_text(encoding="utf-8").splitlines():
            if line.startswith("FROM python"):
                assert BASE.match(line), f"{dockerfile.parent.name}: {line}"
                checked.add(dockerfile.parent.name)
    assert {"api", "connectors", "voice"} <= checked


def test_the_cache_mount_copies_out_of_the_cache() -> None:
    """With the cache on a mount, uv must COPY into the venv: a hardlink or reflink into a
    cache that is not part of the image would leave the venv's files behind."""
    builder = DOCKERFILE.read_text(encoding="utf-8").split("AS runtime", 1)[0]
    assert re.search(r"^\s*UV_LINK_MODE=copy\b", builder, re.M)
    assert "--mount=type=cache,target=/root/.cache/uv" in builder
