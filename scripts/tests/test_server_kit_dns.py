"""Containers lose the link-local range, but not their DNS.

Second review of T7 to T12 (HIGH), confirmed on the Oracle host: its resolver is
`nameserver 169.254.169.254` (/run/systemd/resolve/resolv.conf). Docker's embedded DNS
forwards to that upstream FROM THE CONTAINER'S NAMESPACE, so dropping 169.254.0.0/16 in
DOCKER-USER took DNS from every bridged container and every BuildKit RUN step: no image
build, and an api that reaches no model provider, no Clerk JWKS, no Stripe, no HF.

So the unit lets DNS (udp and tcp 53) to 169.254.169.254 through, ABOVE the drop, and
drops the rest of the range, the metadata service on port 80 included. The rule order is
proved by running the unit's own commands against a fake iptables, twice, and from a
chain that already holds the drop.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
KIT = ROOT / "deploy" / "server"
UNIT = KIT / "systemd" / "open-persona-metadata-block.service"
BASH = shutil.which("bash")

DNS_UDP = "-d 169.254.169.254/32 -p udp --dport 53 -j RETURN"
DNS_TCP = "-d 169.254.169.254/32 -p tcp --dport 53 -j RETURN"
DROP = "-d 169.254.0.0/16 -j DROP"

#: A fake iptables that keeps DOCKER-USER in a file, one rule per line, top first. It
#: understands -C, -I (at the top) and -D (first exact match), which is all the unit uses.
FAKE_IPTABLES = r"""#!/usr/bin/env bash
state="${FAKE_CHAIN:?}"
touch "${state}"
op="$1"; chain="$2"; shift 2
[ "${chain}" = DOCKER-USER ] || exit 3
case "${op}" in
  -I) [ "$1" = 1 ] && shift; rule="$*"
      { printf '%s\n' "${rule}"; cat "${state}"; } > "${state}.new"
      mv "${state}.new" "${state}" ;;
  -C) grep -Fxq -- "$*" "${state}" ;;
  -D) rule="$*"; grep -Fxq -- "${rule}" "${state}" || exit 1
      awk -v r="${rule}" '!done && $0 == r { done = 1; next } { print }' "${state}" > "${state}.new"
      mv "${state}.new" "${state}" ;;
  *) exit 4 ;;
esac
"""


def _directives(name: str) -> list[str]:
    lines = UNIT.read_text(encoding="utf-8").splitlines()
    return [line.split("=", 1)[1] for line in lines if line.startswith(f"{name}=")]


def _shell_body(directive: str) -> str:
    match = re.fullmatch(r"/bin/sh -c '(.*)'", directive)
    assert match, directive
    return match.group(1)


def _run(directives: list[str], tmp_path: Path) -> list[str]:
    assert BASH is not None
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "iptables"
    fake.write_text(FAKE_IPTABLES, encoding="utf-8", newline="\n")
    fake.chmod(0o755)
    chain = tmp_path / "chain"
    env = {
        "PATH": f"{bindir.as_posix()}{os.pathsep}{os.environ['PATH']}",
        "FAKE_CHAIN": chain.as_posix(),
    }
    for directive in directives:
        result = subprocess.run(
            [BASH, "-c", f'PATH="{bindir.as_posix()}:$PATH"; {_shell_body(directive)}'],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        assert result.returncode == 0, (directive, result.stderr)
    return chain.read_text(encoding="utf-8").splitlines() if chain.exists() else []


def test_the_unit_lets_dns_through_and_drops_the_rest_of_the_range() -> None:
    text = UNIT.read_text(encoding="utf-8")
    assert DNS_UDP in text
    assert DNS_TCP in text
    assert DROP in text
    # Nothing else of the range comes back: the metadata service (port 80) stays dropped.
    assert not re.search(r"--dport (80|443)\b", text)
    assert text.count("-j RETURN") == 2 * 3  # check, delete and insert, per DNS rule


needs_bash = pytest.mark.skipif(BASH is None, reason="bash unavailable")


@needs_bash
def test_the_dns_returns_sit_above_the_drop(tmp_path: Path) -> None:
    assert _run(_directives("ExecStart"), tmp_path) == [DNS_UDP, DNS_TCP, DROP]


@needs_bash
def test_a_restart_keeps_one_copy_in_the_same_order(tmp_path: Path) -> None:
    _run(_directives("ExecStart"), tmp_path)
    assert _run(_directives("ExecStart"), tmp_path) == [DNS_UDP, DNS_TCP, DROP]


@needs_bash
def test_a_chain_that_already_drops_the_range_gets_dns_back(tmp_path: Path) -> None:
    """The unit as T7 shipped it left the drop alone; restarting the new one must fix that."""
    (tmp_path / "chain").write_text(f"{DROP}\n", encoding="utf-8", newline="\n")
    assert _run(_directives("ExecStart"), tmp_path) == [DNS_UDP, DNS_TCP, DROP]


@needs_bash
def test_stopping_removes_all_three(tmp_path: Path) -> None:
    _run(_directives("ExecStart"), tmp_path)
    assert _run(_directives("ExecStop"), tmp_path) == []


def test_preflight_proves_dns_and_the_metadata_block_from_a_container() -> None:
    preflight = (KIT / "bin" / "preflight.sh").read_text(encoding="utf-8")
    assert "docker network create" in preflight
    assert "getaddrinfo" in preflight
    assert "169.254.169.254" in preflight
    assert "docker network rm" in preflight
