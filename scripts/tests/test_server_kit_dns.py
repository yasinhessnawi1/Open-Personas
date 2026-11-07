"""Containers lose the link-local range, but not their DNS, and never lose the block.

Second review of T7 to T12 (HIGH), confirmed on the Oracle host: its resolver is
`nameserver 169.254.169.254` (/run/systemd/resolve/resolv.conf). Docker's embedded DNS
forwards to that upstream FROM THE CONTAINER'S NAMESPACE, so dropping 169.254.0.0/16 in
DOCKER-USER took DNS from every bridged container and every BuildKit RUN step. So DNS (udp
and tcp 53) to 169.254.169.254 RETURNs above the drop; port 80 metadata and the rest of the
range stay dropped.

Re-review of 30f85bb9 (LOW): the rules are never removed (no ExecStop: the block must hold
while dockerd is stopped or restarting under live-restore), and a start inserts the new
set first and then deletes older copies below it, so no moment passes without the DROP.

bin/metadata_block.sh is run against a fake iptables that keeps DOCKER-USER in a file and
records the chain after every change.
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
SCRIPT = KIT / "bin" / "metadata_block.sh"
BASH = shutil.which("bash")

DNS_UDP = "-d 169.254.169.254/32 -p udp --dport 53 -j RETURN"
DNS_TCP = "-d 169.254.169.254/32 -p tcp --dport 53 -j RETURN"
DROP = "-d 169.254.0.0/16 -j DROP"
#: What Docker itself may leave in DOCKER-USER, and someone else's rule: both survive.
DOCKER_RETURN = "-j RETURN"
OTHER = "-s 10.9.9.9/32 -j DROP"

#: DOCKER-USER in a file, one rule per line, top first. -I (at the top), -D (by number or
#: by first exact match), -C, and -L -n --line-numbers in iptables' column layout. Every
#: change appends the whole chain to "$FAKE_CHAIN.log", a blank line after each snapshot.
FAKE_IPTABLES = r"""#!/usr/bin/env bash
state="${FAKE_CHAIN:?}"
touch "${state}"
op="$1"; chain="$2"; shift 2
[ "${chain}" = DOCKER-USER ] || exit 3
snapshot() { cat "${state}" >> "${state}.log"; echo >> "${state}.log"; }
case "${op}" in
  -I) [ "$1" = 1 ] && shift
      { printf '%s\n' "$*"; cat "${state}"; } > "${state}.new"
      mv "${state}.new" "${state}"; snapshot ;;
  -C) grep -Fxq -- "$*" "${state}" ;;
  -D) if printf '%s' "$1" | grep -Eq '^[0-9]+$'; then
        [ "$1" -le "$(wc -l < "${state}")" ] || exit 1
        awk -v n="$1" 'NR != n' "${state}" > "${state}.new"
      else
        grep -Fxq -- "$*" "${state}" || exit 1
        awk -v r="$*" '!done && $0 == r { done = 1; next } { print }' "${state}" > "${state}.new"
      fi
      mv "${state}.new" "${state}"; snapshot ;;
  -L) echo "Chain DOCKER-USER (1 references)"
      echo "num  target     prot opt source               destination"
      n=0
      while IFS= read -r rule; do
        n=$((n + 1))
        target="$(printf '%s' "${rule}" | sed -n 's/.*-j \([A-Z]*\).*/\1/p')"
        prot="$(printf '%s' "${rule}" | sed -n 's/.*-p \([a-z]*\).*/\1/p')"
        src="$(printf '%s' "${rule}" | sed -n 's/.*-s \([0-9./]*\).*/\1/p')"
        dst="$(printf '%s' "${rule}" | sed -n 's/.*-d \([0-9./]*\).*/\1/p')"
        # FAKE_NO_OPT: an iptables that prints no `opt` column.
        opt="--  "; [ -z "${FAKE_NO_OPT:-}" ] || opt=""
        printf "%-4s %-10s %-4s ${opt}%-20s %s\n" "${n}" "${target}" "${prot:-all}" \
          "${src:-0.0.0.0/0}" "$(printf '%s' "${dst:-0.0.0.0/0}" | sed 's|/32$||')"
      done < "${state}" ;;
  *) exit 4 ;;
esac
"""

needs_bash = pytest.mark.skipif(BASH is None, reason="bash unavailable")


def _start(
    tmp_path: Path, before: list[str] | None = None, *, no_opt: bool = False
) -> tuple[list[str], list[list[str]]]:
    """Runs the script once; returns the final chain and every intermediate one."""
    assert BASH is not None
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "iptables"
    fake.write_text(FAKE_IPTABLES, encoding="utf-8", newline="\n")
    fake.chmod(0o755)
    chain = tmp_path / "chain"
    if before is not None:
        chain.write_text("".join(f"{r}\n" for r in before), encoding="utf-8", newline="\n")
    log = tmp_path / "chain.log"
    log.unlink(missing_ok=True)
    result = subprocess.run(
        [BASH, "-c", f'. "{SCRIPT.as_posix()}"'],
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_CHAIN": chain.as_posix(),
            **({"FAKE_NO_OPT": "1"} if no_opt else {}),
        },
    )
    assert result.returncode == 0, result.stderr
    snapshots = [s.splitlines() for s in log.read_text(encoding="utf-8").split("\n\n") if s]
    return chain.read_text(encoding="utf-8").splitlines(), snapshots


def test_the_unit_runs_the_script_and_never_removes_the_rules() -> None:
    unit = UNIT.read_text(encoding="utf-8")
    assert "ExecStart=/opt/open-persona/repo/deploy/server/bin/metadata_block.sh" in unit
    assert not [line for line in unit.splitlines() if line.startswith("ExecStop")]
    assert "RemainAfterExit=yes" in unit


def test_the_script_lets_dns_through_and_drops_the_rest_of_the_range() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    for rule in (DNS_UDP, DNS_TCP, DROP):
        assert f"iptables -I DOCKER-USER 1 {rule}" in text
    # Nothing else of the range comes back: the metadata service (port 80) stays dropped.
    assert not re.search(r"--dport (80|443)\b", text)
    assert text.count("-j RETURN") == 2


@needs_bash
def test_a_fresh_chain_gets_the_returns_above_the_drop(tmp_path: Path) -> None:
    final, _ = _start(tmp_path)
    assert final == [DNS_UDP, DNS_TCP, DROP]


@needs_bash
def test_a_restart_keeps_one_copy_and_never_drops_the_drop(tmp_path: Path) -> None:
    _start(tmp_path)
    final, snapshots = _start(tmp_path)
    assert final == [DNS_UDP, DNS_TCP, DROP]
    assert all(DROP in s for s in snapshots), snapshots


@needs_bash
def test_the_old_units_chain_gets_dns_back_without_a_gap(tmp_path: Path) -> None:
    """The unit as T7 shipped it left only the drop."""
    final, snapshots = _start(tmp_path, [DROP])
    assert final == [DNS_UDP, DNS_TCP, DROP]
    assert all(DROP in s for s in snapshots), snapshots


@needs_bash
@pytest.mark.parametrize("no_opt", [False, True], ids=["opt-column", "no-opt-column"])
def test_other_rules_in_the_chain_survive(tmp_path: Path, no_opt: bool) -> None:
    final, _ = _start(tmp_path, [DNS_UDP, OTHER, DNS_TCP, DROP, DROP, DOCKER_RETURN], no_opt=no_opt)
    assert final == [DNS_UDP, DNS_TCP, DROP, OTHER, DOCKER_RETURN]


def test_the_script_is_installed_as_a_kit_script() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail" in text


def test_preflight_proves_dns_and_the_metadata_block_from_a_container() -> None:
    preflight = (KIT / "bin" / "preflight.sh").read_text(encoding="utf-8")
    assert "docker network create" in preflight
    assert "getaddrinfo" in preflight
    assert "169.254.169.254" in preflight
    assert "docker network rm" in preflight
