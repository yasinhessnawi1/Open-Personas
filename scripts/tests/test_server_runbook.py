"""The server kit's RUNBOOK tells the Oracle core host's story, and its commands work.

Review of T1 to T6 (MEDIUM): the RUNBOOK still told the Hetzner single-box story (the
Hetzner console, opening LiveKit's ports, voice and livekit DNS, a voice health check, a
rollback tagging voice and livekit, HETZNER_* secrets) and its manual compose commands
bypassed the host profile. An operator follows this file line by line at the restore, so
what it says must match what the kit does.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
KIT = ROOT / "deploy" / "server"
RUNBOOK = (KIT / "RUNBOOK.md").read_text(encoding="utf-8")
CORE_PART, _, PHASE_B = RUNBOOK.partition("\n## Phase B")


def test_it_is_the_core_hosts_runbook() -> None:
    title = RUNBOOK.splitlines()[0]
    assert title.startswith("# ")
    assert "Oracle" in title
    assert PHASE_B, "the RUNBOOK needs a '## Phase B' section for voice on Azure"


def test_manual_compose_commands_go_through_the_profile() -> None:
    assert "docker compose -f" not in RUNBOOK
    assert "docker compose " not in RUNBOOK.replace("`docker compose config`", "")
    assert RUNBOOK.count("bin/compose.sh ") >= 5


def test_the_wrapper_applies_the_profile() -> None:
    wrapper = (KIT / "bin" / "compose.sh").read_text(encoding="utf-8")
    assert '. "${SCRIPT_DIR}/lib/compose.sh"' in wrapper
    assert re.search(r'^compose "\$@"$', wrapper, re.M)


def test_the_core_steps_carry_nothing_of_the_single_box() -> None:
    for stale in (
        "Hetzner console",
        "HETZNER_",
        "7881",
        "7882",
        "voice listener",
        "for i in api voice livekit",
        "selfhosted-livekit",
        "CX42",
        "Storage Box",
    ):
        assert stale not in CORE_PART, stale


def test_dns_for_the_core_host_names_only_its_two_hostnames() -> None:
    dns = CORE_PART.split("## Step 8", 1)[1].split("\n## ", 1)[0]
    core = ("api.openpersonas.si", "connectors.openpersonas.si")
    for host in core:
        assert host in dns, host
    for host in ("voice.openpersonas.si", "livekit.openpersonas.si"):
        assert f"{host} " not in dns.split("```")[1], f"{host} gets a record in step 8"


def test_it_names_what_the_oracle_host_needs() -> None:
    for needle in (
        "--profile oracle",
        "--public-ipv4",
        "/dev/oracleoci/oraclevdb",
        "## The block volume",
        "PERSONA_VOICE_SERVICE_URL",
        "off-site copy PENDING",
        "R9-285",
        "uid 2001",
        "/home/ubuntu",
        "IMDSv2",
    ):
        assert needle in RUNBOOK, needle


def test_rendering_for_the_core_host_does_not_ask_for_voice_files() -> None:
    renders = re.findall(r"render_env\.py --dotenv \.env --out \S+[^\n]*", CORE_PART)
    assert renders
    assert all("--host all" not in line for line in renders)


def test_every_script_it_names_exists() -> None:
    for script in set(re.findall(r"\bbin/[a-z_/]+\.sh\b", RUNBOOK)):
        assert (KIT / script).is_file(), script


def test_the_seam_and_the_rollback_survive() -> None:
    assert "SEAM: per-tenant MCP runtime" in RUNBOOK
    rollback = RUNBOOK.split("## Rolling back", 1)[1].split("\n## ", 1)[0]
    assert "open-persona/api:previous open-persona/api:current" in rollback


def test_every_script_is_executable_in_git() -> None:
    """The host runs bin/*.sh directly; a file committed without +x (easy from Windows)
    fails there with "permission denied"."""
    import subprocess

    listed = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-s", "deploy/server/bin"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    modes = {line.split()[3]: line.split()[0] for line in listed}
    scripts = [name for name in modes if re.fullmatch(r"deploy/server/bin/[a-z_]+\.sh", name)]
    assert "deploy/server/bin/compose.sh" in scripts
    assert all(modes[name] == "100755" for name in scripts), modes


def test_every_command_block_says_which_machine_it_runs_on() -> None:
    """Second review (LOW): an operator must never guess whether a block is for the PC or
    the host. Every ```bash block opens with `# on the PC` or `# on the host`."""
    blocks = re.findall(r"```bash\n(.*?)```", RUNBOOK, re.S)
    assert blocks
    for block in blocks:
        first = block.strip().splitlines()[0]
        assert re.match(r"# on the (PC|host)\b", first), first


def test_the_uid_separation_is_described_honestly() -> None:
    """Second review (MEDIUM): ubuntu has passwordless sudo, so uid 2001 stops accidents,
    not anything running as ubuntu."""
    who = RUNBOOK.split("## Who can do what", 1)[1].split("\n## ", 1)[0]
    assert "guards against accidents" in who
    assert "root-equivalent" in who.split("uid 2001", 1)[1]


def test_the_persona_id_check_reaches_the_operator() -> None:
    """R9-285: a persona id outside the minted format can never be opened or deleted."""
    assert "personas_unexpected_ids" in (KIT / "sql" / "verify.sql").read_text(encoding="utf-8")
    assert "personas_unexpected_ids" in (KIT / "bin" / "restore.sh").read_text(encoding="utf-8")
    assert "personas_unexpected_ids|0" in RUNBOOK


def test_the_orphan_count_reaches_the_operator_with_a_way_to_settle_it() -> None:
    """R9-284: a restored database can hold memory of personas already deleted."""
    assert "memory_orphan_owners" in (KIT / "sql" / "verify.sql").read_text(encoding="utf-8")
    assert "memory_orphan_owners" in (KIT / "bin" / "restore.sh").read_text(encoding="utf-8")
    step = RUNBOOK.split("### Sweep what deleted personas left behind", 1)[1].split("\n## ", 1)[0]
    assert "memory_orphan_owners" in step
    assert "--orphaned" in step
    assert "--apply" in step
    assert "persona repair --all-personas" in step


CONTAINERD_MOVE = RUNBOOK.split("### containerd's root (R9-289)", 1)[1].split("\n## ", 1)[0]
CONTAINERD_CONFIG = "/etc/containerd/config.toml"
CONTAINERD_BAK = "/etc/containerd/config.toml.bak-r9289"


def _containerd_script(number: int) -> str:
    """Step <number> of the containerd move as pasted: up to the next step or the fence."""
    found = re.search(
        rf"^# {number}\. .*?(?=^# {number + 1}\. |^```)", CONTAINERD_MOVE, re.S | re.M
    )
    assert found, f"step {number}"
    return found.group(0)


def _containerd_step(number: int) -> str:
    """Step <number> of the containerd move, as code: comments dropped, continued lines
    joined, whitespace collapsed."""
    code = "\n".join(
        line
        for line in _containerd_script(number).splitlines()
        if not line.lstrip().startswith("#")
    )
    return " ".join(code.replace("\\\n", " ").split())


def test_the_containerd_moves_backup_is_not_cp_n() -> None:
    """R9-289 round 3 MEDIUM-1: coreutils 9.2 to 9.4 (Ubuntu 24.04 ships 9.4) make `cp -n`
    exit 1 when it skips an existing file. On a redo after a rollback the backup is there,
    so `cp -an ... && sed` would never prepend the root and the move would go on without it.
    """
    step = _containerd_step(6)
    backup = (
        f"{{ sudo test -e {CONTAINERD_BAK} || sudo cp -a {CONTAINERD_CONFIG} {CONTAINERD_BAK}; }}"
        f" && sudo sed -i '1i root = \"/srv/containerd\"' {CONTAINERD_CONFIG}"
    )
    assert backup in step
    assert not re.search(r"\bcp\s+-[a-zA-Z]*n[a-zA-Z]*\s[^&]*&&", step), "cp -n ... &&"


BASH = shutil.which("bash")
PACKAGE_CONFIG = 'disabled_plugins = ["cri"]\n\n#root = "/var/lib/containerd"\n'
MOVED_LINE = 'root = "/srv/containerd"\n'


@pytest.mark.skipif(BASH is None, reason="bash unavailable")
@pytest.mark.parametrize(
    ("config", "bak", "said", "config_after", "bak_after"),
    [
        (PACKAGE_CONFIG, None, "", MOVED_LINE + PACKAGE_CONFIG, PACKAGE_CONFIG),
        # A redo after a rollback: the first move's backup is kept, the root set again.
        (PACKAGE_CONFIG, "earlier", "", MOVED_LINE + PACKAGE_CONFIG, "earlier"),
        (
            MOVED_LINE + PACKAGE_CONFIG,
            PACKAGE_CONFIG,
            "step 6 already done, go on to step 7",
            MOVED_LINE + PACKAGE_CONFIG,
            PACKAGE_CONFIG,
        ),
        (
            'root = "/var/lib/other"\n' + PACKAGE_CONFIG,
            None,
            "STOP: containerd's config sets another root; do not go on",
            'root = "/var/lib/other"\n' + PACKAGE_CONFIG,
            None,
        ),
    ],
    ids=["first move", "redo after a rollback", "pasted twice", "another root"],
)
def test_the_containerd_moves_step_6_says_what_it_found(
    tmp_path: Path,
    config: str,
    bak: str | None,
    said: str,
    config_after: str,
    bak_after: str | None,
) -> None:
    """R9-289 round 3 LOW-1: step 6 run as pasted, on a config under tmp_path. A second
    paste says it is done and goes on; only a root this procedure did not set says STOP."""
    target = tmp_path / "config.toml"
    target.write_text(config, encoding="utf-8", newline="\n")
    backup = tmp_path / "config.toml.bak-r9289"
    if bak is not None:
        backup.write_text(bak, encoding="utf-8", newline="\n")
    script = _containerd_script(6).replace(CONTAINERD_CONFIG, target.as_posix())
    assert BASH is not None
    result = subprocess.run(
        [BASH, "-c", 'sudo() { "$@"; }\nset -u\n' + script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == said
    assert target.read_text(encoding="utf-8") == config_after
    assert (backup.read_text(encoding="utf-8") if backup.exists() else None) == bak_after


def test_the_containerd_move_is_pasted_one_step_at_a_time() -> None:
    """R9-289 round 3 LOW-1: step 6 can print STOP, so it is never pasted inside a block
    with the steps after it: it has a block of its own, and the text says so."""
    assert "Paste the numbered steps one at a time, never a block whole" in CONTAINERD_MOVE
    blocks = re.findall(r"```bash\n(.*?)```", CONTAINERD_MOVE, re.S)
    step_6 = next(block for block in blocks if re.search(r"^# 6\. ", block, re.M))
    assert re.findall(r"^# (\d+)\. ", step_6, re.M) == ["6"]
