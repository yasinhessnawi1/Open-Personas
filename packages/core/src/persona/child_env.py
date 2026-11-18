"""The allow-listed environment a subprocess of the api starts with (R9-258).

The api process holds database DSNs, model provider keys, Stripe and Clerk keys and
connector tokens. Its subprocesses (the built-in MCP children, the git clones of the
catalog syncs) need none of them, so they must not inherit them: a compromised child
could read its own environment, and a same-uid child could read a sibling's
``/proc/<pid>/environ`` while it runs.

The rule is default deny. A child gets exactly:

- the names in :data:`BASE_CHILD_ENV`, which every such child needs to start
  (interpreter, OS runtime, locale, temp dir, the baked tiktoken cache, logging);
- the names its own kind adds: a built-in MCP server declares ``CHILD_ENV`` in its
  module, collected in :data:`persona.tools.mcp.builtin.SERVER_CHILD_ENV`;
- any value the caller sets for that one spawn (``overrides``), such as the
  filesystem scope root.

Names are listed one by one. No name passes by prefix or pattern, so a new secret
added to the api env can never reach a child by accident. A listed name the api env
lacks is simply absent in the child, as it was before this rule.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "BASE_CHILD_ENV",
    "GIT_CHILD_ENV",
    "OUTBOUND_HTTPS_ENV",
    "build_child_env",
    "git_env",
]

#: POSIX process basics. USER and LOGNAME matter: the data uid has no passwd entry
#: in the image, so getpass.getuser() needs them (R9-292).
_POSIX = frozenset({"PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "TZ"})

#: Locale, read by Python's text encoding and gettext at start.
_LOCALE = frozenset(
    {
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "LC_CTYPE",
        "LC_MESSAGES",
        "LC_NUMERIC",
        "LC_TIME",
        "LC_COLLATE",
        "LC_MONETARY",
    }
)

#: Windows runtime. Without SYSTEMROOT a child cannot initialise Winsock, so it
#: could not even bind its port; the rest locate temp, profile and site dirs.
_WINDOWS = frozenset(
    {
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "USERNAME",
        "HOMEDRIVE",
        "HOMEPATH",
        "APPDATA",
        "LOCALAPPDATA",
        "PROCESSOR_ARCHITECTURE",
        "PROCESSOR_ARCHITEW6432",
    }
)

#: The interpreter's own settings, so the child runs the same Python the api runs
#: (PYTHONPATH covers a source checkout; the image sets the two bytecode/buffer ones).
_PYTHON = frozenset(
    {
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONUTF8",
        "PYTHONIOENCODING",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONUNBUFFERED",
        "PYTHONNOUSERSITE",
        "PYTHONUSERBASE",
        "PYTHONSAFEPATH",
        "PYTHONPYCACHEPREFIX",
        "PYTHONTZPATH",
        "VIRTUAL_ENV",
    }
)

#: App settings every child reads. Each child imports tiktoken (through
#: ``persona.tools``), which needs the encoding the image bakes at TIKTOKEN_CACHE_DIR,
#: or it downloads it at import. The first log call builds the logging sinks from
#: the three PERSONA_LOG_* settings.
_APP = frozenset(
    {
        "TIKTOKEN_CACHE_DIR",
        "DATA_GYM_CACHE_DIR",
        "PERSONA_LOG_LEVEL",
        "PERSONA_LOG_FORMAT",
        "PERSONA_LOG_FILE",
    }
)

#: The names every built-in MCP child receives from the api env, when set there.
BASE_CHILD_ENV: frozenset[str] = _POSIX | _LOCALE | _WINDOWS | _PYTHON | _APP

#: What a child that calls out over HTTPS needs: the CA bundle names and the proxy
#: names (both cases), so it still gets out where the operator routes egress through
#: a proxy. A proxy URL can carry proxy credentials; it is allowed only to the
#: children that call out. SSLKEYLOGFILE is deliberately not here.
OUTBOUND_HTTPS_ENV: frozenset[str] = frozenset(
    {
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    }
)

#: What ``git clone`` over https needs beyond the base: the outbound names, git's own
#: CA settings, and where it finds the operator's git config (HOME is in the base).
#: No credential: every clone here is anonymous https, and the only place a
#: credential could travel is the clone URL itself (argv), which callers pass as is.
GIT_CHILD_ENV: frozenset[str] = OUTBOUND_HTTPS_ENV | frozenset(
    {"GIT_SSL_CAINFO", "GIT_SSL_CAPATH", "XDG_CONFIG_HOME"}
)

#: Set on every git run: there is no tty, so a credential prompt could only hang.
_GIT_OVERRIDES = {"GIT_TERMINAL_PROMPT": "0"}

#: Windows env names are case-insensitive, so there a name matches whatever its case.
_FOLD_CASE = sys.platform == "win32"


def build_child_env(
    extra: frozenset[str],
    parent: Mapping[str, str],
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return a new env holding only the allow-listed names of ``parent``, plus ``overrides``.

    Args:
        extra: The server's own names, added to :data:`BASE_CHILD_ENV`.
        parent: The api env (normally ``os.environ``). It is not changed.
        overrides: Values the launcher sets for this one spawn. They are added as
            given, whatever the allow-list says.

    Windows env names are case-insensitive, so there a name matches whatever its
    case; elsewhere a name must match exactly.
    """
    allowed = BASE_CHILD_ENV | extra
    env = {
        name: value
        for name, value in parent.items()
        if name in allowed or (_FOLD_CASE and name.upper() in allowed)
    }
    return {**env, **(overrides or {})}


def git_env(parent: Mapping[str, str] | None = None) -> dict[str, str]:
    """The env to run ``git`` with: the base, :data:`GIT_CHILD_ENV`, and no prompt.

    Args:
        parent: The api env; defaults to ``os.environ``. It is not changed.
    """
    import os

    source = os.environ if parent is None else parent
    return build_child_env(GIT_CHILD_ENV, source, _GIT_OVERRIDES)
