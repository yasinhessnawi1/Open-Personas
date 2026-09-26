"""Windows lexical path rules for the sandbox resolver (Spec WIN, T1.3).

Pure string checks, no filesystem call of any kind. On Windows the sandbox resolver
runs them BEFORE anything touches the filesystem, because on Windows merely
resolving some paths has side effects: a UNC path (``\\\\host\\share``) or a link to
one makes Windows contact that host and authenticate to it (an NTLM hash leak), and
device paths (``\\\\.\\``, ``\\\\?\\``, ``\\??\\``) reach devices and the object manager.

The rules also close Win32 name aliasing, where the name that is checked is not the
file that is opened: a trailing dot or space is silently dropped (``a.txt.`` opens
``a.txt``), ``name:stream`` writes a hidden alternate data stream, and reserved device
names (``NUL``, ``CON``, ``COM1`` ... with or without an extension, depending on the
Windows version) open a device instead of a file (a write to ``NUL`` "succeeds" and
the data is lost).

The module is platform-neutral so its rules are tested everywhere; only the
resolver's ``sys.platform == "win32"`` branch calls it.
"""

from __future__ import annotations

__all__ = ["windows_component_violation", "windows_path_violation"]

#: Characters Windows does not allow in a file or folder name. ``:`` also covers a
#: drive (``C:x``) and an alternate data stream (``name:stream``). ``/`` is the
#: separator the sandbox accepts and ``\\`` has its own, clearer reason.
_RESERVED_CHARACTERS = frozenset(':<>"|?*')

#: Reserved device names, matched case-insensitively on the part before the first dot
#: with trailing spaces removed (so ``nul.txt`` and ``COM1 .log`` match). ``COM0`` and
#: ``LPT0`` and the superscript-digit forms are included: older Windows versions and
#: some APIs treat them as devices too, and refusing them costs nothing.
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"{device}{digit}" for device in ("COM", "LPT") for digit in "0123456789\u00b9\u00b2\u00b3"}
)


def windows_path_violation(requested: str) -> tuple[str, str] | None:
    """The first Windows-specific reason ``requested`` must be refused, or ``None``.

    Args:
        requested: The caller-supplied relative path, with ``/`` as its separator.

    Returns:
        ``(summary, reason)`` for the resolver's ``SandboxViolationError`` (the reason
        keys a human correction hint), or ``None`` when no Windows rule is broken. The
        platform-neutral checks (NUL byte, length, empty, absolute, escape) stay in
        the resolver itself.
    """
    if "\\" in requested:
        # Catches UNC (\\host\share), device (\\.\, \\?\, \??\), drive-rooted (C:\x)
        # and root-relative (\x) forms too, before any of them can reach the system.
        return "backslash in path", "mixed_separators"
    if len(requested) >= 2 and requested[1] == ":" and requested[0].isalpha():
        return "drive path not allowed", "absolute"
    if any(c in _RESERVED_CHARACTERS or ord(c) < 32 for c in requested):
        return "character not allowed in Windows file names", "reserved_character"
    for component in requested.split("/"):
        violation = windows_component_violation(component)
        if violation is not None:
            return violation
    return None


def windows_component_violation(component: str) -> tuple[str, str] | None:
    """The rule one path component breaks, or ``None``; ``""``, ``.`` and ``..`` are left
    to the resolver's own normalisation and escape check."""
    if component in ("", ".", ".."):
        return None
    if component[-1] in ". ":
        return "name ends with a dot or a space", "trailing_dot_or_space"
    if component.partition(".")[0].rstrip(" ").upper() in _RESERVED_NAMES:
        return "reserved Windows device name", "reserved_name"
    return None
