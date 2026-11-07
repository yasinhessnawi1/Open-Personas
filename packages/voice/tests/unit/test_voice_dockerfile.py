"""The voice image builds for linux/amd64 and linux/arm64 from one Dockerfile.

The builder clears the executable-stack flag on every shared object in the venv with
patchelf 0.18 (the apt and pip builds are too old for ``--clear-execstack``), fetched as a
static release binary and verified by sha256. It used to fetch the x86_64 binary only, so
an arm64 build failed there. ``packages/voice/docker/patchelf.sh`` now picks the binary
for the build's architecture, with a pinned digest for each; these tests run it in its
print-only mode, so they need a POSIX shell but no network and no Docker.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_VOICE = Path(__file__).resolve().parents[2]
_SCRIPT = _VOICE / "docker" / "patchelf.sh"
_DOCKERFILE = _VOICE / "Dockerfile"
_RELEASES = "https://github.com/NixOS/patchelf/releases/download/0.18.0"

#: sha256 of the patchelf 0.18.0 release tarballs, computed from the downloads (GitHub
#: publishes no digest for these assets). The aarch64 binary was checked to be EM_AARCH64.
_PINNED = {
    "amd64": (
        f"{_RELEASES}/patchelf-0.18.0-x86_64.tar.gz",
        "ce84f2447fb7a8679e58bc54a20dc2b01b37b5802e12c57eece772a6f14bf3f0",
    ),
    "arm64": (
        f"{_RELEASES}/patchelf-0.18.0-aarch64.tar.gz",
        "ae13e2effe077e829be759182396b931d8f85cfb9cfe9d49385516ea367ef7b2",
    ),
}

_SH = shutil.which("sh")
needs_sh = pytest.mark.skipif(_SH is None, reason="needs a POSIX sh")


def _print_only(arch: str) -> subprocess.CompletedProcess[str]:
    assert _SH is not None
    env = {**os.environ, "PATCHELF_PRINT_ONLY": "1"}
    return subprocess.run(
        [_SH, str(_SCRIPT), arch], capture_output=True, text=True, env=env, check=False
    )


@needs_sh
@pytest.mark.parametrize("arch", sorted(_PINNED))
def test_each_supported_architecture_gets_its_own_pinned_binary(arch: str) -> None:
    result = _print_only(arch)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == list(_PINNED[arch])


@needs_sh
@pytest.mark.parametrize("arch", ["", "386", "riscv64", "x86_64"])
def test_an_unsupported_architecture_stops_the_build(arch: str) -> None:
    result = _print_only(arch)
    assert result.returncode != 0
    assert "unsupported architecture" in result.stderr
    assert result.stdout == ""


def _dockerfile_text() -> str:
    # One logical line per instruction: join backslash continuations.
    return re.sub(r"\\\n\s*", " ", _DOCKERFILE.read_text(encoding="utf-8"))


def test_the_dockerfile_fetches_patchelf_for_the_target_architecture() -> None:
    text = _dockerfile_text()
    assert "ARG TARGETARCH" in text
    step = 'sh /app/packages/voice/docker/patchelf.sh "${TARGETARCH:-$(dpkg --print-architecture)}"'
    assert step in text, "the patchelf step must pass the target architecture to patchelf.sh"
    assert "x86_64.tar.gz" not in text, "no architecture may be hardcoded in the Dockerfile"


def test_the_dockerfile_proves_the_native_libraries_load_on_the_target() -> None:
    text = _dockerfile_text()
    smoke = re.search(r"RUN /opt/venv/bin/python -I -c (.+)", text)
    assert smoke is not None, "the builder must load the native libraries before the image ships"
    assert "get_ffi_lib()" in smoke.group(1)  # LiveKit's FFI library, through dlopen
    assert "SileroVAD(16000)" in smoke.group(1)  # ONNX Runtime and the bundled model
    # The smoke runs after the exec-stack pass, against the venv the runtime stage copies.
    assert text.index("--clear-execstack") < text.index("RUN /opt/venv/bin/python -I -c")
