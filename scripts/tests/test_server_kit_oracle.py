"""The server kit on the Oracle core host: provisioning, the profile in the scripts, the data uid.

The Oracle server differs from the box the kit was written for (2026-11-07 facts):

* arm64, reached as `ubuntu` (uid 1000, passwordless sudo) whose home holds the owner's
  other work: so the operator is `deploy` (uid 2000) next to `ubuntu`, never instead of
  it, and the app's data is owned by uid 2001, not 1000, so `ubuntu`'s processes cannot
  read tenant files;
* Oracle's image ships iptables rules (netfilter-persistent) that REJECT every inbound
  port but 22 ahead of anything ufw adds, and an `InstanceServices` OUTPUT chain that
  keeps non-root processes off the iSCSI and metadata endpoints. ufw takes over INPUT;
  the InstanceServices rules are carried over verbatim from the host's own file;
* rpcbind listens on 111 and the unified-monitoring-agent runs: both are masked (ruling
  D5); oracle-cloud-agent stays (it reports the metrics Oracle's idle check reads);
* a 45 GB boot volume shared with the owner's work, and a separate 100 GB block volume
  (/dev/oracleoci/oraclevdb, ruling D3) that holds /srv: the data, the backups, Docker's
  data root and the swapfile. Nothing may write production data to the boot disk because
  the block volume failed to mount.

Behaviour is tested by running the bash helpers where bash exists; the rest pins the
scripts' text the way test_server_kit.py does.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import yaml

if TYPE_CHECKING:
    from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
KIT = ROOT / "deploy" / "server"
BIN = KIT / "bin"
LIB = BIN / "lib"
BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="bash unavailable")


def _text(relative: str) -> str:
    return (KIT / relative).read_text(encoding="utf-8")


def _profile() -> dict[str, str]:
    values = {}
    for line in _text("hosts/oracle.env").splitlines():
        if line and not line.startswith("#"):
            name, _, value = line.partition("=")
            values[name] = value
    return values


def _bash(script: str, **env: str) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    return subprocess.run(
        [BASH, "-c", script],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": os.environ["PATH"], **env},
    )


# --- the Oracle profile --------------------------------------------------------------------


def test_the_oracle_profile_describes_the_host() -> None:
    values = _profile()
    assert values["OP_EXPECT_ARCH"] == "aarch64"
    assert values["OP_DATA_DEVICE"] == "/dev/oracleoci/oraclevdb"
    assert values["OP_DATA_MOUNT"] == "/srv"
    assert values["OP_DOCKER_DATA_ROOT"].startswith("/srv/")
    assert values["OP_ORACLE_FIREWALL"] == "1"
    assert int(values["OP_SWAP_GB"]) >= 2
    assert int(values["OP_BACKUP_MIN_FREE_GB"]) >= 10


def test_the_data_uid_is_neither_ubuntu_nor_the_operator() -> None:
    uid = int(_profile()["OP_DATA_UID"])
    assert uid == 2001
    assert uid not in {0, 1000, 2000}


def test_ssh_admits_the_operator_and_keeps_the_owners_login() -> None:
    users = _profile()["OP_SSH_ALLOW_USERS"].split(",")
    assert "deploy" in users
    assert "ubuntu" in users  # the owner's other work lives under it: never locked out
    assert "root" not in users


def test_the_masked_units_keep_oracle_cloud_agent() -> None:
    prefixes = _profile()["OP_DISABLE_UNIT_PREFIXES"].split(",")
    assert {"rpcbind", "unified-monitoring-agent"} <= set(prefixes)
    assert not any(p.startswith("oracle-cloud-agent") for p in prefixes)


# --- the profile loader (bin/lib/profile.sh) ------------------------------------------------


def _kit_with_profile(tmp_path: Path, body: str, name: str = "t") -> Path:
    (tmp_path / "hosts").mkdir()
    (tmp_path / "hosts" / f"{name}.env").write_text(body, encoding="utf-8", newline="\n")
    return tmp_path


@needs_bash
def test_the_loader_exports_the_profile_without_overriding_the_caller(tmp_path: Path) -> None:
    kit = _kit_with_profile(tmp_path, "# a comment\nOP_A=one\nOP_B=/srv/x,y:z\nOP_C=three\n")
    result = _bash(
        f'KIT_DIR="{kit.as_posix()}"; . "{(LIB / "profile.sh").as_posix()}"; '
        'echo "$OP_A|$OP_B|$OP_C|$OP_HOST_PROFILE"',
        OP_HOST_PROFILE="t",
        OP_C="from-caller",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "one|/srv/x,y:z|from-caller|t"


@needs_bash
@pytest.mark.parametrize(
    "body",
    ["OP_A=$(id)\n", "OP_A=two words\n", "PATH=/tmp\n", "OP_A='quoted'\n", "OP_A=x;id\n"],
)
def test_the_loader_refuses_anything_but_plain_settings(tmp_path: Path, body: str) -> None:
    kit = _kit_with_profile(tmp_path, body)
    result = _bash(
        f'KIT_DIR="{kit.as_posix()}"; . "{(LIB / "profile.sh").as_posix()}"; echo loaded',
        OP_HOST_PROFILE="t",
    )
    assert result.returncode != 0
    assert "loaded" not in result.stdout


@needs_bash
@pytest.mark.parametrize("name", ["../t", "T", "t/x", ""])
def test_the_loader_refuses_a_profile_name_outside_hosts(tmp_path: Path, name: str) -> None:
    kit = _kit_with_profile(tmp_path, "OP_A=one\n")
    result = _bash(
        f'KIT_DIR="{kit.as_posix()}"; OP_HOST_PROFILE="{name}"; '
        f'[ -n "$OP_HOST_PROFILE" ] || OP_HOST_PROFILE=missing; '
        f'. "{(LIB / "profile.sh").as_posix()}"; echo loaded',
    )
    assert result.returncode != 0
    assert "loaded" not in result.stdout


@needs_bash
def test_require_data_mount_refuses_a_directory_that_is_not_a_mount(tmp_path: Path) -> None:
    if shutil.which("mountpoint") is None:
        pytest.skip("mountpoint unavailable")
    kit = _kit_with_profile(tmp_path, f"OP_DATA_MOUNT={(tmp_path / 'srv').as_posix()}\n")
    (tmp_path / "srv").mkdir()
    lib = (LIB / "profile.sh").as_posix()
    refused = _bash(
        f'KIT_DIR="{kit.as_posix()}"; . "{lib}"; require_data_mount; echo passed',
        OP_HOST_PROFILE="t",
    )
    assert refused.returncode != 0
    assert "passed" not in refused.stdout
    unset = _bash(
        f'KIT_DIR="{kit.as_posix()}"; . "{lib}"; OP_DATA_MOUNT=; require_data_mount; echo passed',
        OP_HOST_PROFILE="t",
    )
    assert unset.stdout.strip() == "passed"


def test_compose_sources_the_loader() -> None:
    assert '. "${KIT_DIR}/bin/lib/profile.sh"' in (LIB / "compose.sh").read_text(encoding="utf-8")


@pytest.mark.parametrize("script", ["deploy.sh", "backup.sh", "restore.sh"])
def test_scripts_that_write_data_require_the_block_volume(script: str) -> None:
    assert "require_data_mount" in _text(f"bin/{script}")


# --- Oracle's firewall, handed to ufw (bin/lib/oracle_firewall.sh) --------------------------

ORACLE_RULES_V4 = """# CLOUD_IMG: This file was created/modified by the Cloud Image build process
*filter
:INPUT ACCEPT [0:0]
:FORWARD ACCEPT [0:0]
:OUTPUT ACCEPT [0:0]
:InstanceServices - [0:0]
-A INPUT -m state --state RELATED,ESTABLISHED -j ACCEPT
-A INPUT -p icmp -j ACCEPT
-A INPUT -i lo -j ACCEPT
-A INPUT -p tcp -m state --state NEW -m tcp --dport 22 -j ACCEPT
-A INPUT -j REJECT --reject-with icmp-host-prohibited
-A FORWARD -j REJECT --reject-with icmp-host-prohibited
-A OUTPUT -d 169.254.0.0/16 -j InstanceServices
-A InstanceServices -d 169.254.0.2/32 -p tcp -m owner --uid-owner 0 -m tcp --dport 3260 -j ACCEPT
-A InstanceServices -d 169.254.169.254/32 -p udp -m udp --dport 123 -j ACCEPT
-A InstanceServices -d 169.254.0.0/16 -p tcp -m tcp -j REJECT --reject-with tcp-reset
COMMIT
"""

UFW_BEFORE_RULES = """# rules.before
*filter
:ufw-before-input - [0:0]
:ufw-before-output - [0:0]
-A ufw-before-input -i lo -j ACCEPT
-A ufw-before-output -o lo -j ACCEPT
COMMIT
"""


def _firewall(tmp_path: Path, call: str) -> subprocess.CompletedProcess[str]:
    rules = tmp_path / "rules.v4"
    before = tmp_path / "before.rules"
    if not rules.exists():
        rules.write_text(ORACLE_RULES_V4, encoding="utf-8", newline="\n")
    if not before.exists():
        before.write_text(UFW_BEFORE_RULES, encoding="utf-8", newline="\n")
    return _bash(
        f'. "{(LIB / "oracle_firewall.sh").as_posix()}"; '
        f'R="{rules.as_posix()}"; B="{before.as_posix()}"; {call}'
    )


@needs_bash
def test_the_instance_services_rules_are_copied_verbatim_and_nothing_else(tmp_path: Path) -> None:
    result = _firewall(tmp_path, 'instance_services_rules "$R"')
    assert result.returncode == 0, result.stderr
    lines = result.stdout.strip().splitlines()
    assert lines[0] == "-A ufw-before-output -d 169.254.0.0/16 -j InstanceServices"
    assert lines[1:] == [
        line for line in ORACLE_RULES_V4.splitlines() if line.startswith("-A InstanceServices")
    ]
    assert "icmp-host-prohibited" not in result.stdout


@needs_bash
def test_the_merge_declares_the_chain_and_adds_the_rules_before_commit(tmp_path: Path) -> None:
    result = _firewall(tmp_path, 'merge_before_rules "$B" "$(instance_services_rules "$R")"')
    assert result.returncode == 0, result.stderr
    merged = result.stdout.splitlines()
    declare = merged.index(":InstanceServices - [0:0]")
    jump = merged.index("-A ufw-before-output -d 169.254.0.0/16 -j InstanceServices")
    assert merged.index("*filter") < declare < merged.index(":ufw-before-input - [0:0]") + 3
    assert declare < jump < merged.index("COMMIT")
    assert merged[-1] == "COMMIT"
    # Every original line survives, in order.
    kept = [line for line in merged if line in UFW_BEFORE_RULES.splitlines()]
    assert kept == UFW_BEFORE_RULES.splitlines()


@needs_bash
def test_the_merge_is_idempotent(tmp_path: Path) -> None:
    once = _firewall(tmp_path, 'merge_before_rules "$B" "$(instance_services_rules "$R")"')
    (tmp_path / "before.rules").write_text(once.stdout, encoding="utf-8", newline="\n")
    twice = _firewall(tmp_path, 'merge_before_rules "$B" "$(instance_services_rules "$R")"')
    assert twice.stdout == once.stdout
    assert twice.stdout.count(":InstanceServices - [0:0]") == 1


@needs_bash
def test_nothing_is_merged_when_the_host_has_no_instance_services(tmp_path: Path) -> None:
    (tmp_path / "rules.v4").write_text("*filter\nCOMMIT\n", encoding="utf-8", newline="\n")
    result = _firewall(tmp_path, 'merge_before_rules "$B" "$(instance_services_rules "$R")"')
    assert result.stdout == UFW_BEFORE_RULES


def test_provision_hands_oracles_firewall_to_ufw() -> None:
    provision = _text("bin/provision.sh")
    assert '. "${SCRIPT_DIR}/lib/oracle_firewall.sh"' in provision
    for needle in (
        "/etc/ufw/before.rules",
        "systemctl mask netfilter-persistent",
        "icmp-host-prohibited",
        "/root/open-persona-firewall-backup",
    ):
        assert needle in provision, needle


# --- provisioning the Oracle host ------------------------------------------------------------


def test_provision_checks_the_profiles_architecture_not_x86() -> None:
    provision = _text("bin/provision.sh")
    assert '"$(uname -m)" = "${OP_EXPECT_ARCH}"' in provision
    assert "x86_64 only" not in provision


def test_docker_comes_from_the_repository_for_the_hosts_architecture() -> None:
    provision = _text("bin/provision.sh")
    assert "arch=$(dpkg --print-architecture)" in provision
    assert "arch=amd64" not in provision


def test_provision_records_the_profile_and_the_public_address() -> None:
    provision = _text("bin/provision.sh")
    assert "/etc/open-persona/host-profile" in provision
    assert "/etc/open-persona/public-ipv4" in provision


def test_the_operator_gets_the_invoking_users_key_on_oracle() -> None:
    provision = _text("bin/provision.sh")
    assert "SUDO_USER" in provision
    assert "OP_UID=2000" in provision


def test_ssh_allow_list_comes_from_the_profile() -> None:
    provision = _text("bin/provision.sh")
    assert "OP_SSH_ALLOW_USERS" in provision
    assert not re.search(r"^AllowUsers deploy$", provision, re.M)
    assert "PermitRootLogin no" in provision


def test_the_block_volume_is_formatted_only_when_blank_and_mounted_by_uuid() -> None:
    """Second review (LOW): only blkid's exit 2 means blank; any other failure refuses."""
    provision = _text("bin/provision.sh")
    probe = 'blkid -p "${dev}" >/dev/null 2>&1 || blkid_rc=$?'
    assert probe in provision
    case = provision[provision.index(probe) : provision.index("esac", provision.index(probe))]
    # exit 0: a signature, accepted only if it is our own; exit 2: blank, formatted;
    # anything else: refused, never formatted.
    assert re.search(r"\n\s+0\)", case)
    assert re.search(r"\n\s+2\)", case)
    assert re.search(r"\n\s+\*\) die ", case)
    assert case.index("mkfs.ext4") > case.index("2)")
    assert case.index("mkfs.ext4") < case.index("*) die ")
    assert "UUID=" in provision
    assert "nofail" in provision


def test_an_existing_mount_must_be_the_op_data_volume() -> None:
    """Second review (LOW): /srv already mounted is accepted only as our own volume."""
    provision = _text("bin/provision.sh")
    start = provision.index('if mountpoint -q "${OP_DATA_MOUNT}"; then')
    branch = provision[start : provision.index("return 0", start)]
    assert "LABEL" in branch
    assert "op-data" in branch
    assert 'readlink -f "${OP_DATA_DEVICE}"' in branch
    assert "die " in branch


def test_ssh_must_admit_the_user_running_provision() -> None:
    """Second review (LOW): a missing invoking user is a refusal, not a warning."""
    provision = _text("bin/provision.sh")
    start = provision.index('case " ${allow_users} " in\n    *" ${SUDO_USER} "*)')
    branch = provision[start : provision.index("esac", start)]
    assert "die " in branch
    assert "WARNING" not in branch


def test_the_ssh_hold_comment_says_when_ufws_limit_takes_effect() -> None:
    # Comments wrap: compare with the comment markers and line breaks folded away.
    flat = " ".join(_text("bin/provision.sh").replace("#", " ").split())
    assert "Oracle's own 22 ACCEPT stays ahead of ufw's chains until the reboot" in flat


def test_deploy_creates_the_api_home_as_the_data_uid() -> None:
    """HOME=/var/lib/persona/home must exist and belong to the data uid before the api starts.
    Created by deploy, not provision: restore.sh refuses to unpack into a non-empty dir."""
    deploy = _text("bin/deploy.sh")
    create = (
        'sudo install -d -o "${OP_DATA_UID}" -g "${OP_DATA_UID}" -m 0700'
        ' "${OP_DATA_DIR}/persona/home"'
    )
    assert create in deploy
    assert deploy.index(create) < deploy.index("compose up -d --remove-orphans")
    assert "persona/home" not in _text("bin/provision.sh")


def test_docker_lives_on_the_block_volume_and_waits_for_it() -> None:
    provision = _text("bin/provision.sh")
    assert '"data-root": "${OP_DOCKER_DATA_ROOT}"' in provision
    assert "RequiresMountsFor=${OP_DATA_MOUNT}" in provision


def test_swap_journald_and_masked_units() -> None:
    provision = _text("bin/provision.sh")
    assert "OP_SWAP_GB" in provision
    assert "mkswap" in provision
    assert "vm.swappiness" in provision
    assert "SystemMaxUse=" in provision
    assert "OP_DISABLE_UNIT_PREFIXES" in provision
    assert "systemctl mask" in provision
    # Even a profile naming it cannot get oracle-cloud-agent masked.
    assert re.search(r"oracle-cloud-agent\*\) die", provision)


def test_the_data_directories_belong_to_the_data_uid_and_voice_has_none() -> None:
    provision = _text("bin/provision.sh")
    assert '-o "${OP_DATA_UID}" -g "${OP_DATA_UID}"' in provision
    assert "DATA_UID=1000" not in provision
    assert "for d in persona voice" not in provision


def test_containers_lose_the_whole_link_local_range() -> None:
    """The whole range is dropped; the only way back is DNS (test_server_kit_dns.py)."""
    script = _text("bin/metadata_block.sh")
    assert "iptables -I DOCKER-USER 1 -d 169.254.0.0/16 -j DROP" in script
    for line in script.splitlines():
        if "169.254.169.254/32" in line and line.startswith("iptables"):
            assert "--dport 53 -j RETURN" in line, line


# --- the data uid in compose and restore -----------------------------------------------------


def _compose() -> dict[str, Any]:
    return yaml.safe_load(_text("docker-compose.yml"))


@pytest.mark.parametrize("service", ["api", "migrate"])
def test_the_api_image_runs_as_the_data_uid(service: str) -> None:
    definition = _compose()["services"][service]
    assert definition["user"] == "${OP_DATA_UID:-2001}:${OP_DATA_UID:-2001}"
    assert definition["environment"]["HOME"].startswith(("/var/lib/persona/", "/tmp"))


def test_restore_unpacks_as_the_profiles_data_uid() -> None:
    restore = _text("bin/restore.sh")
    assert 'DATA_UID="${OP_DATA_UID:-2001}"' in restore
    assert "DATA_UID=1000" not in restore


def test_no_script_hardcodes_uid_1000_as_the_data_owner() -> None:
    for script in BIN.rglob("*.sh"):
        assert not re.search(r"DATA_UID=1000\b", script.read_text(encoding="utf-8")), script.name


# --- backups: local now, off-site when its target exists (ruling D4) --------------------------


def test_the_off_site_target_is_configuration() -> None:
    unit = _text("systemd/open-persona-backup.service")
    assert "EnvironmentFile=-/opt/open-persona/env/offsite.env" in unit
    template = _text("env/offsite.env.example")
    assert re.search(r"^OFFSITE_CMD=$", template, re.M)


def test_the_backup_says_when_the_off_site_copy_is_pending() -> None:
    backup = _text("bin/backup.sh")
    assert "off-site copy PENDING" in backup


# --- preflight on the Oracle host ------------------------------------------------------------


def test_preflight_checks_what_provision_set_up() -> None:
    preflight = _text("bin/preflight.sh")
    for needle in (
        "mountpoint -q",
        "OP_DOCKER_DATA_ROOT",
        "InstanceServices",
        "icmp-host-prohibited",
        "/etc/open-persona/public-ipv4",
        "OP_DATA_UID",
        "OP_EXPECT_ARCH",
        "OP_DISABLE_UNIT_PREFIXES",
    ):
        assert needle in preflight, needle
    # The chain is checked in the RUNNING firewall, and DNS is compared with the recorded
    # public address (OCI's interface carries only the private one).
    assert "if sudo iptables -S InstanceServices >/dev/null 2>&1; then" in preflight
    assert "ip4=\"$(tr -d '[:space:]' < /etc/open-persona/public-ipv4" in preflight


# --- render_env renders only what the host needs --------------------------------------------


def _render() -> ModuleType:
    spec = importlib.util.spec_from_file_location("server_render_env_oracle", KIT / "render_env.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_core_host_gets_no_voice_or_livekit_secrets(tmp_path: Path) -> None:
    render = _render()
    dotenv = tmp_path / ".env"
    dotenv.write_text("PERSONA_OPENROUTER_API_KEY=x\n", encoding="utf-8")
    out = tmp_path / "out"
    assert render.main(["--dotenv", str(dotenv), "--out", str(out)]) == 0
    written = {p.name for p in out.iterdir()}
    assert "voice.env" not in written
    assert "livekit.env" not in written
    assert {"common.env", "api.env", "postgres.env", "migrate.env", "caddy.env"} <= written
    assert {"alert.env", "offsite.env"} <= written


def test_all_templates_render_when_asked(tmp_path: Path) -> None:
    render = _render()
    dotenv = tmp_path / ".env"
    dotenv.write_text("PERSONA_OPENROUTER_API_KEY=x\n", encoding="utf-8")
    out = tmp_path / "all"
    assert render.main(["--dotenv", str(dotenv), "--out", str(out), "--host", "all"]) == 0
    assert {"voice.env", "livekit.env"} <= {p.name for p in out.iterdir()}


# --- the ufw install removes iptables-persistent (seen on the real host, 2026-11-07) -------
# `apt-get -s install ufw` on the Oracle image: ufw 0.36.2-6 in, iptables-persistent and
# netfilter-persistent 1.0.20 OUT. So the chain is read before apt runs, nothing after it
# assumes netfilter-persistent exists, and SSH is held open explicitly across the install.

_HOLD_RULE = "-p tcp --dport 22 -m comment --comment open-persona-ssh-hold -j ACCEPT"
SSH_HOLD = f"iptables -I INPUT 1 {_HOLD_RULE}"
SSH_RELEASE = f"iptables -D INPUT {_HOLD_RULE}"


def test_the_firewall_steps_run_in_a_safe_order() -> None:
    provision = _text("bin/provision.sh")
    steps = [
        'cp -p "${f}" "${FW_BACKUP}/"',  # 1. keep Oracle's rules
        'instance_rules="$(instance_services_rules',  # 2. read the chain from the copy
        SSH_HOLD,  # 3. hold SSH open
        "apt-get install -y -q ufw",  # 4. the install that removes iptables-persistent
        'merge_before_rules /etc/ufw/before.rules "${instance_rules}"',  # 5. into ufw
        "ufw --force enable",  # 6. ufw owns INPUT, 22 limited
        SSH_RELEASE,  # 7. the hold goes, ufw's limit applies
    ]
    positions = [provision.find(step) for step in steps]
    assert all(p >= 0 for p in positions), [
        s for s, p in zip(steps, positions, strict=True) if p < 0
    ]
    assert positions == sorted(positions), positions


def test_the_chain_is_kept_on_disk_before_apt_runs() -> None:
    provision = _text("bin/provision.sh")
    saved = provision.find('> "${FW_BACKUP}/instance-services.rules"')
    assert 0 <= saved < provision.find("apt-get install -y -q ufw")


def test_a_removed_netfilter_persistent_does_not_fail_the_script() -> None:
    provision = _text("bin/provision.sh")
    check = (
        "if systemctl list-unit-files --no-legend netfilter-persistent.service | grep -q .; then"
    )
    assert check in provision
    branch = provision[provision.index(check) : provision.index("\n  fi\n", provision.index(check))]
    assert "systemctl mask netfilter-persistent" in branch
    assert "else" in branch
