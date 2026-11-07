"""The native libraries a call loads must load on a current Linux, on both architectures.

Two ways a native library fails to load only on some machines, both read here from the ELF
file itself rather than discovered at call time:

* **An executable stack.** glibc 2.41 and later refuse to ``dlopen`` a library whose
  ``PT_GNU_STACK`` segment asks for one ("cannot enable executable stack as shared object
  requires"). That is the glibc of the voice image's ``python:3.12-slim`` base and of the
  ``ubuntu-latest`` runner. ``silero-vad-lite`` 0.2.x shipped such a library; a missing
  header counts too, because the loader then assumes an executable stack.
* **The wrong architecture.** Voice supports Linux x86_64 and aarch64. A wheel (or a
  source build) that carries an x86_64 library onto an aarch64 machine fails at load.

The libraries checked are the ones the voice runtime loads natively: ONNX Runtime (the
Silero VAD session) and LiveKit's FFI library (the WebRTC transport). The ELF reader is
tested on synthetic headers on every platform, so a broken reader cannot pass silently; the
check of the installed libraries runs on Linux only, where the files are ELF.
"""

from __future__ import annotations

import importlib.util
import platform
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

_ELF_MAGIC = b"\x7fELF"
_ELFCLASS64 = 2
_ELFDATA2LSB = 1
_PT_GNU_STACK = 0x6474E551
_PF_X = 0x1
_PF_R = 0x4
_PF_W = 0x2

#: ``e_machine`` per ``platform.machine()`` on the Linux architectures voice supports.
_EM_FOR_MACHINE = {"x86_64": 62, "aarch64": 183}


@dataclass(frozen=True)
class ElfFacts:
    """The two loader-relevant facts of a 64-bit little-endian ELF file."""

    machine: int
    gnu_stack_flags: int | None


def read_elf_facts(image: bytes) -> ElfFacts:
    """Read ``e_machine`` and the ``PT_GNU_STACK`` flags (None when the header is absent).

    Raises:
        ValueError: the bytes are not a 64-bit little-endian ELF file.
    """
    if image[:4] != _ELF_MAGIC:
        raise ValueError("not an ELF file")
    if image[4] != _ELFCLASS64 or image[5] != _ELFDATA2LSB:
        raise ValueError("expected a 64-bit little-endian ELF file")
    (machine,) = struct.unpack_from("<H", image, 0x12)
    (program_headers_offset,) = struct.unpack_from("<Q", image, 0x20)
    entry_size, entry_count = struct.unpack_from("<HH", image, 0x36)
    stack_flags: int | None = None
    for index in range(entry_count):
        kind, flags = struct.unpack_from("<II", image, program_headers_offset + index * entry_size)
        if kind == _PT_GNU_STACK:
            stack_flags = int(flags)
    return ElfFacts(machine=int(machine), gnu_stack_flags=stack_flags)


def load_problems(facts: ElfFacts, *, expected_machine: int) -> list[str]:
    """Why the loader would refuse this library on the expected machine (empty when it loads)."""
    problems: list[str] = []
    if facts.machine != expected_machine:
        problems.append(f"built for e_machine {facts.machine}, this machine is {expected_machine}")
    if facts.gnu_stack_flags is None:
        problems.append("no PT_GNU_STACK header: the loader assumes an executable stack")
    elif facts.gnu_stack_flags & _PF_X:
        problems.append("PT_GNU_STACK is executable: glibc 2.41+ refuses to load it")
    return problems


def _package_dir(name: str) -> Path:
    spec = importlib.util.find_spec(name)
    assert spec is not None, f"{name} is not installed"
    assert spec.origin is not None, f"{name} has no origin"
    return Path(spec.origin).parent


def voice_native_libraries() -> list[Path]:
    """The shared objects the voice runtime loads: ONNX Runtime and LiveKit's FFI library."""
    onnxruntime_capi = _package_dir("onnxruntime") / "capi"
    libraries = sorted(p for p in onnxruntime_capi.iterdir() if ".so" in p.name)
    libraries.append(_package_dir("livekit.rtc") / "resources" / "liblivekit_ffi.so")
    return libraries


# ---------- the reader, on synthetic headers (every platform) ----------------------


def _synthetic_elf(*, machine: int, stack_flags: int | None, magic: bytes = _ELF_MAGIC) -> bytes:
    """A minimal ELF64 LE image: a 64-byte header, then a table of 56-byte program headers."""
    headers = [(1, _PF_R | _PF_X)]  # a PT_LOAD, so the stack header is not simply the first
    if stack_flags is not None:
        headers.append((_PT_GNU_STACK, stack_flags))
    ident = magic + bytes([_ELFCLASS64, _ELFDATA2LSB, 1]) + bytes(9)
    header = ident + struct.pack("<HHI", 3, machine, 1)  # ET_DYN, e_machine, EV_CURRENT
    header += struct.pack("<QQQ", 0, 64, 0)  # e_entry, e_phoff, e_shoff
    header += struct.pack("<IHHHHHH", 0, 64, 56, len(headers), 64, 0, 0)
    table = b"".join(struct.pack("<II", kind, flags) + bytes(48) for kind, flags in headers)
    return header + table


def test_the_reader_finds_the_machine_and_a_non_executable_stack() -> None:
    facts = read_elf_facts(_synthetic_elf(machine=183, stack_flags=_PF_R | _PF_W))
    assert facts == ElfFacts(machine=183, gnu_stack_flags=_PF_R | _PF_W)
    assert load_problems(facts, expected_machine=183) == []


def test_an_executable_stack_is_a_load_problem() -> None:
    facts = read_elf_facts(_synthetic_elf(machine=62, stack_flags=_PF_R | _PF_W | _PF_X))
    assert load_problems(facts, expected_machine=62) == [
        "PT_GNU_STACK is executable: glibc 2.41+ refuses to load it"
    ]


def test_a_missing_stack_header_is_a_load_problem() -> None:
    facts = read_elf_facts(_synthetic_elf(machine=62, stack_flags=None))
    assert facts.gnu_stack_flags is None
    assert load_problems(facts, expected_machine=62) == [
        "no PT_GNU_STACK header: the loader assumes an executable stack"
    ]


def test_a_library_for_the_other_architecture_is_a_load_problem() -> None:
    facts = read_elf_facts(_synthetic_elf(machine=62, stack_flags=_PF_R | _PF_W))
    assert load_problems(facts, expected_machine=183) == [
        "built for e_machine 62, this machine is 183"
    ]


def test_the_reader_refuses_what_is_not_elf() -> None:
    with pytest.raises(ValueError, match="not an ELF file"):
        read_elf_facts(_synthetic_elf(machine=62, stack_flags=0, magic=b"MZ\x90\x00"))


# ---------- the installed libraries (Linux only) ------------------------------------


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the libraries are ELF on Linux")
def test_every_native_library_a_call_loads_fits_this_machine() -> None:
    expected = _EM_FOR_MACHINE.get(platform.machine())
    assert expected is not None, (
        f"voice supports x86_64 and aarch64 Linux, not {platform.machine()}"
    )
    libraries = voice_native_libraries()
    names = {path.name for path in libraries}
    assert any(name.startswith("onnxruntime_pybind11_state") for name in names), names
    assert "liblivekit_ffi.so" in names, names
    problems = {
        str(path): found
        for path in libraries
        if (found := load_problems(read_elf_facts(path.read_bytes()), expected_machine=expected))
    }
    assert problems == {}
