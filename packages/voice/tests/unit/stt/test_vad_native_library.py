"""The bundled Silero library must load on a current Linux (the voice image and CI).

``silero-vad-lite`` 0.2.x shipped a Linux ``.so`` whose ``PT_GNU_STACK`` segment asks
for an executable stack. glibc 2.41 and later refuse to ``dlopen`` such a library
("cannot enable executable stack as shared object requires"), which is the glibc of
the voice image's ``python:3.12-slim`` base and of the ``ubuntu-latest`` runner. The
load then fails only on those systems, so this test reads the flag itself: it fails
on any Linux, whatever its glibc, if a release that asks for an executable stack is
ever locked again.

The library is an ELF file only on Linux; macOS and Windows ship their own formats
and have no such flag, so the test skips there.
"""

from __future__ import annotations

import importlib.util
import struct
import sys
from pathlib import Path

import pytest

_PT_GNU_STACK = 0x6474E551
_PF_X = 0x1
_ELF_MAGIC = b"\x7fELF"
_ELFCLASS64 = 2
_ELFDATA2LSB = 1


def _silero_library() -> Path:
    spec = importlib.util.find_spec("silero_vad_lite")
    assert spec is not None, "silero-vad-lite is not installed"
    assert spec.origin is not None
    return Path(spec.origin).parent / "data" / "silero_vad_lite.so"


def _gnu_stack_flags(image: bytes) -> int | None:
    """The ``p_flags`` of the ``PT_GNU_STACK`` program header, or None when absent."""
    assert image[:4] == _ELF_MAGIC, "not an ELF file"
    assert image[4] == _ELFCLASS64, "expected a 64-bit ELF file"
    assert image[5] == _ELFDATA2LSB, "expected a little-endian ELF file"
    (program_headers_offset,) = struct.unpack_from("<Q", image, 0x20)
    entry_size, entry_count = struct.unpack_from("<HH", image, 0x36)
    for index in range(entry_count):
        kind, flags = struct.unpack_from("<II", image, program_headers_offset + index * entry_size)
        if kind == _PT_GNU_STACK:
            return int(flags)
    return None


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the library is ELF on Linux")
def test_the_bundled_silero_library_does_not_ask_for_an_executable_stack() -> None:
    flags = _gnu_stack_flags(_silero_library().read_bytes())
    # A missing header means "executable stack" to the loader, so it is refused too.
    assert flags is not None, "no PT_GNU_STACK header: the loader assumes an executable stack"
    assert not flags & _PF_X, "PT_GNU_STACK is executable: glibc 2.41+ refuses to load it"
