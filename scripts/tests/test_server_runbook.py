"""The server kit's RUNBOOK tells the Oracle core host's story, and its commands work.

Review of T1 to T6 (MEDIUM): the RUNBOOK still told the Hetzner single-box story (the
Hetzner console, opening LiveKit's ports, voice and livekit DNS, a voice health check, a
rollback tagging voice and livekit, HETZNER_* secrets) and its manual compose commands
bypassed the host profile. An operator follows this file line by line at the restore, so
what it says must match what the kit does.
"""

from __future__ import annotations

import re
from pathlib import Path

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
