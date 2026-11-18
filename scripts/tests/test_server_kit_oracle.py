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


@pytest.mark.parametrize("service", ["api", "migrate"])
def test_the_data_uid_has_a_user_name_in_the_environment(service: str) -> None:
    # The data uid has no passwd entry in the image, so getpass.getuser() falls through to
    # pwd.getpwuid() and raises KeyError. torch calls it on import (its cache dir), which left
    # torch._dynamo half registered and made every persona creation answer 500 on Oracle.
    # getpass.getuser() reads USER before it ever asks pwd.
    environment = _compose()["services"][service]["environment"]
    assert environment.get("USER")
    assert environment.get("LOGNAME") == environment.get("USER")


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


# --- containerd's root on the block volume (R9-289) ------------------------------------------


def test_the_oracle_profile_puts_containerds_root_on_the_block_volume() -> None:
    assert _profile()["OP_CONTAINERD_ROOT"] == "/srv/containerd"


def _containerd(tmp_path: Path, config: str | None) -> subprocess.CompletedProcess[str]:
    path = tmp_path / "config.toml"
    if config is not None:
        path.write_text(config, encoding="utf-8", newline="\n")
    return _bash(
        f'set -eu; . "{(LIB / "containerd_root.sh").as_posix()}"; '
        f'containerd_config_with_root "{path.as_posix()}" /srv/containerd'
    )


# The containerd.io package's config, abridged: comments and the one top-level key.
CONTAINERD_DEFAULT = (
    "#   Copyright 2018-2022 Docker Inc.\n"
    "\n"
    'disabled_plugins = ["cri"]\n'
    "\n"
    '#root = "/var/lib/containerd"\n'
)


@needs_bash
def test_containerds_root_is_prepended_above_the_packages_config(tmp_path: Path) -> None:
    result = _containerd(tmp_path, CONTAINERD_DEFAULT)
    assert result.returncode == 0, result.stderr
    assert result.stdout == 'root = "/srv/containerd"\n' + CONTAINERD_DEFAULT


@needs_bash
def test_setting_containerds_root_twice_changes_nothing(tmp_path: Path) -> None:
    once = _containerd(tmp_path, CONTAINERD_DEFAULT)
    twice = _containerd(tmp_path, once.stdout)
    assert twice.returncode == 0, twice.stderr
    assert twice.stdout == once.stdout


@needs_bash
def test_another_root_already_set_is_refused(tmp_path: Path) -> None:
    result = _containerd(tmp_path, 'root = "/var/lib/containerd"\n')
    assert result.returncode == 1
    assert "already sets root to /var/lib/containerd" in result.stderr
    assert result.stdout == ""


@needs_bash
def test_a_root_inside_a_table_is_not_containerds_root(tmp_path: Path) -> None:
    config = 'version = 2\n\n[plugins."io.containerd.grpc.v1.cri"]\n  root = "/x"\n'
    result = _containerd(tmp_path, config)
    assert result.returncode == 0, result.stderr
    assert result.stdout == 'root = "/srv/containerd"\n' + config


@needs_bash
def test_a_missing_config_becomes_just_the_root(tmp_path: Path) -> None:
    result = _containerd(tmp_path, None)
    assert result.returncode == 0, result.stderr
    assert result.stdout == 'root = "/srv/containerd"\n'


def test_provision_sets_containerds_root_before_docker_restarts() -> None:
    provision = _text("bin/provision.sh")
    lib = provision.index('. "${SCRIPT_DIR}/lib/containerd_root.sh"')
    apply = provision.index("apply_containerd_root /etc/containerd/config.toml")
    assert lib < apply < provision.index("systemctl restart docker")
    call = provision[apply : provision.index("\n", provision.index("|| die", apply))]
    assert '"${OP_CONTAINERD_ROOT}" "${OP_DATA_MOUNT:-}"' in call
    assert "containerd.service.d/10-open-persona.conf" in call


# docker info answers unless docker.down exists; ps -aq and images -q print their own files
# (review M1: a stub that cannot tell them apart could not catch a guard that asks only one).
STUB_DOCKER = (
    "#!/usr/bin/env bash\n"
    'case "$1" in\n'
    '  info) [ -e "${STUB_DIR}/docker.down" ] && exit 1; exit 0 ;;\n'
    '  ps) cat "${STUB_DIR}/ps.out" 2>/dev/null || true ;;\n'
    '  images) cat "${STUB_DIR}/images.out" 2>/dev/null || true ;;\n'
    "esac\n"
)
STUB_SYSTEMCTL = '#!/usr/bin/env bash\necho "$*" >> "${STUB_DIR}/systemctl.log"\n'
# `install -d -m MODE DIR`, as the lib calls it: the directory, without the chmod (which Git
# Bash cannot do under a Windows temp directory).
STUB_INSTALL = '#!/usr/bin/env bash\nmkdir -p "${@: -1}"\n'
MOVED = 'root = "/srv/containerd"\n' + CONTAINERD_DEFAULT
DROPIN = "[Unit]\nRequiresMountsFor=/srv\n"


def _failing_stub(name: str, match: str) -> str:
    """A stub that fails only the call whose arguments contain ``match``.

    Every other call goes on to the real command (the stub's directory leads PATH), or, for
    install, to the mkdir that STUB_INSTALL does.
    """
    rest = {
        "install": 'mkdir -p "${@: -1}"',
        "systemctl": 'echo "$*" >> "${STUB_DIR}/systemctl.log"',
    }.get(name, f'PATH="${{PATH#*:}}" exec {name} "$@"')
    return f'#!/usr/bin/env bash\ncase " $* " in *{match}*) exit 1 ;; esac\n{rest}\n'


def _apply(
    tmp_path: Path,
    *,
    config: str | None,
    images: str,
    dropin: str | None,
    failing: tuple[tuple[str, str], ...] = (),
    containers: str = "",
    docker_down: bool = False,
    held: str | None = None,
    bak: str | None = None,
    stuck_dropin_tmp: bool = False,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """apply_containerd_root on files under tmp_path, with docker and systemctl stubbed."""
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for name, body in (
        ("docker", STUB_DOCKER),
        ("systemctl", STUB_SYSTEMCTL),
        ("install", STUB_INSTALL),
        *((name, _failing_stub(name, match)) for name, match in failing),
    ):
        (stubs / name).write_text(body, encoding="utf-8", newline="\n")
        (stubs / name).chmod(0o755)
    (stubs / "images.out").write_text(images, encoding="utf-8", newline="\n")
    (stubs / "ps.out").write_text(containers, encoding="utf-8", newline="\n")
    if docker_down:
        (stubs / "docker.down").touch()
    old_root = tmp_path / "var-lib-containerd"
    if held is not None:  # a file in this directory under the old root
        (old_root / held).mkdir(parents=True)
        (old_root / held / "x").write_text("layer", encoding="utf-8")
    etc = tmp_path / "etc"
    etc.mkdir()
    if config is not None:
        (etc / "config.toml").write_text(config, encoding="utf-8", newline="\n")
    if dropin is not None:
        (etc / "dropin.conf").write_text(dropin, encoding="utf-8", newline="\n")
    if bak is not None:
        (etc / "config.toml.bak-r9289").write_text(bak, encoding="utf-8", newline="\n")
    if stuck_dropin_tmp:
        # A directory where the drop-in's temporary goes: its printf fails for root too
        # (round 3 LOW-5: root writes through a 0444 file, so that failed for no one but us).
        (etc / "dropin.conf.tmp").mkdir()
    result = _bash(
        # A POSIX path for PATH: a "C:/..." entry would split at its colon.
        f'set -eu; export STUB_DIR="{stubs.as_posix()}"; '
        f'export CONTAINERD_DEFAULT_ROOT="{old_root.as_posix()}"; '
        f'PATH="$(cd "{stubs.as_posix()}" && pwd):$PATH"; '
        f'. "{(LIB / "containerd_root.sh").as_posix()}"; '
        f'apply_containerd_root "{(etc / "config.toml").as_posix()}" '
        f'"{(tmp_path / "srv-containerd").as_posix()}" /srv "{(etc / "dropin.conf").as_posix()}" '
        # As provision.sh calls it, in a `||` list: errexit is off inside, as it is there, so
        # only the function's own `|| return 1` can stop it (review M2).
        "|| exit 1"
    )
    log = stubs / "systemctl.log"
    return result, log.read_text(encoding="utf-8").splitlines() if log.exists() else []


@needs_bash
def test_a_host_already_on_the_root_with_images_is_a_clean_no_op(tmp_path: Path) -> None:
    """The Oracle host, moved by hand on 2026-11-16: the root is set and Docker holds images.

    Re-running provision must neither die on the images nor touch or restart anything.
    """
    root = (tmp_path / "srv-containerd").as_posix()
    config = MOVED.replace("/srv/containerd", root)
    result, systemctl = _apply(tmp_path, config=config, images="sha256:abc\n", dropin=DROPIN)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "etc" / "config.toml").read_text(encoding="utf-8") == config
    assert systemctl == [], "no reload, no restart"
    assert not (tmp_path / "srv-containerd").exists(), "nothing created"


@needs_bash
def test_a_root_to_move_under_images_is_refused_and_nothing_changes(tmp_path: Path) -> None:
    result, systemctl = _apply(
        tmp_path, config=CONTAINERD_DEFAULT, images="sha256:abc\n", dropin=None
    )
    assert result.returncode == 1
    assert "Docker holds containers or images" in result.stderr
    assert (tmp_path / "etc" / "config.toml").read_text(encoding="utf-8") == CONTAINERD_DEFAULT
    assert systemctl == []


@needs_bash
def test_a_fresh_host_gets_the_root_the_drop_in_and_one_restart(tmp_path: Path) -> None:
    root = (tmp_path / "srv-containerd").as_posix()
    result, systemctl = _apply(tmp_path, config=CONTAINERD_DEFAULT, images="", dropin=None)
    assert result.returncode == 0, result.stderr
    config = (tmp_path / "etc" / "config.toml").read_text(encoding="utf-8")
    assert config == f'root = "{root}"\n' + CONTAINERD_DEFAULT
    assert (tmp_path / "etc" / "dropin.conf").read_text(encoding="utf-8") == DROPIN
    assert (tmp_path / "srv-containerd").is_dir()
    assert systemctl == ["daemon-reload", "restart containerd"]


@needs_bash
def test_the_live_hosts_first_re_run_reloads_and_never_restarts(tmp_path: Path) -> None:
    """Review H1, the Oracle host as it is: root set by hand, no drop-in, images present.

    The re-run writes the drop-in and reloads systemd; containerd keeps running.
    """
    root = (tmp_path / "srv-containerd").as_posix()
    config = MOVED.replace("/srv/containerd", root)
    result, systemctl = _apply(tmp_path, config=config, images="sha256:abc\n", dropin=None)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "etc" / "config.toml").read_text(encoding="utf-8") == config
    assert (tmp_path / "etc" / "dropin.conf").read_text(encoding="utf-8") == DROPIN
    assert systemctl == ["daemon-reload"], "no restart under the running app"


@needs_bash
def test_the_config_is_moved_into_place_and_the_original_kept(tmp_path: Path) -> None:
    """Review M2: written whole to config.toml.tmp and moved; the package's config kept."""
    result, _ = _apply(tmp_path, config=CONTAINERD_DEFAULT, images="", dropin=None)
    assert result.returncode == 0, result.stderr
    etc = tmp_path / "etc"
    assert (etc / "config.toml.bak-r9289").read_text(encoding="utf-8") == CONTAINERD_DEFAULT
    assert not (etc / "config.toml.tmp").exists()
    assert not (etc / "config.toml.bak-r9289.tmp").exists()
    assert not (etc / "dropin.conf.tmp").exists()


@needs_bash
@pytest.mark.parametrize(
    "broken",
    [
        ("install", "srv-containerd"),  # the new root
        ("cp", "bak-r9289"),  # the original's copy
        ("mv", "bak-r9289.tmp"),  # ...moved into place
        ("chmod", "config.toml.tmp"),
        ("mv", "config.toml.tmp"),
    ],
    ids=lambda b: b[0],
)
def test_a_step_that_fails_fails_the_whole_and_restarts_nothing(
    tmp_path: Path, broken: tuple[str, str]
) -> None:
    """Review M2: under `|| die` errexit is off, so each step must stop the function itself.

    Each stub fails one call only, so it is that step's own check that stops the function,
    not a later step's; the drop-in (already in place) adds nothing to restart.
    """
    result, systemctl = _apply(
        tmp_path, config=CONTAINERD_DEFAULT, images="", dropin=DROPIN, failing=(broken,)
    )
    assert result.returncode == 1
    assert (tmp_path / "etc" / "config.toml").read_text(encoding="utf-8") == CONTAINERD_DEFAULT
    assert systemctl == []


@needs_bash
@pytest.mark.parametrize(
    ("signal", "said"),
    [
        ({"containers": "c0ffee\n"}, "Docker holds containers or images"),
        ({"docker_down": True}, "does not answer"),
        ({"held": "io.containerd.content.v1.content/blobs/sha256"}, "holds image content"),
        ({"held": "io.containerd.snapshotter.v1.overlayfs/snapshots"}, "holds image content"),
    ],
    ids=["containers only", "docker down", "blobs on disk", "snapshots on disk"],
)
def test_the_move_is_refused_whatever_says_something_is_there(
    tmp_path: Path, signal: dict[str, Any], said: str
) -> None:
    """Review M1: containers alone, an unreachable Docker, or content on the old root's disk."""
    result, systemctl = _apply(
        tmp_path, config=CONTAINERD_DEFAULT, images="", dropin=None, **signal
    )
    assert result.returncode == 1
    assert said in result.stderr
    assert (tmp_path / "etc" / "config.toml").read_text(encoding="utf-8") == CONTAINERD_DEFAULT
    assert systemctl == []


@needs_bash
def test_empty_content_directories_do_not_stop_the_move(tmp_path: Path) -> None:
    old_root = tmp_path / "var-lib-containerd"
    for d in (
        "io.containerd.content.v1.content/blobs/sha256",
        "io.containerd.snapshotter.v1.overlayfs/snapshots",
    ):
        (old_root / d).mkdir(parents=True)
    result, systemctl = _apply(tmp_path, config=CONTAINERD_DEFAULT, images="", dropin=None)
    assert result.returncode == 0, result.stderr
    assert systemctl == ["daemon-reload", "restart containerd"]


BOM = "\ufeff"


@needs_bash
def test_a_byte_order_mark_is_dropped_above_the_new_root(tmp_path: Path) -> None:
    """Review L1: the root line must be the file's first, not after an invisible mark."""
    result = _containerd(tmp_path, BOM + CONTAINERD_DEFAULT)
    assert result.returncode == 0, result.stderr
    assert result.stdout == 'root = "/srv/containerd"\n' + CONTAINERD_DEFAULT


@needs_bash
def test_a_root_set_after_a_byte_order_mark_is_no_change(tmp_path: Path) -> None:
    root = (tmp_path / "srv-containerd").as_posix()
    config = BOM + MOVED.replace("/srv/containerd", root)
    result, systemctl = _apply(tmp_path, config=config, images="sha256:abc\n", dropin=DROPIN)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "etc" / "config.toml").read_text(encoding="utf-8") == config
    assert systemctl == []


@needs_bash
@pytest.mark.parametrize(
    ("config", "said"),
    [
        ('"root" = "/var/lib/containerd"\n', "already sets root to /var/lib/containerd"),
        ("root = '/srv/containerd'\n", "cannot read"),
        ("root = /srv/containerd\n", "cannot read"),
        ('root = ""\n', "empty value"),
    ],
    ids=["quoted key", "single quotes", "bare value", "empty"],
)
def test_a_root_this_cannot_take_as_set_is_refused_never_prepended(
    tmp_path: Path, config: str, said: str
) -> None:
    result = _containerd(tmp_path, config)
    assert result.returncode == 1
    assert said in result.stderr
    assert result.stdout == ""


@needs_bash
def test_a_quoted_root_key_set_to_the_root_is_no_change(tmp_path: Path) -> None:
    config = '"root" = "/srv/containerd"\n' + CONTAINERD_DEFAULT
    result = _containerd(tmp_path, config)
    assert result.returncode == 0, result.stderr
    assert result.stdout == config


def test_preflight_checks_containerds_root_against_the_profile() -> None:
    """Review L2: the root the running containerd reads, compared with OP_CONTAINERD_ROOT."""
    preflight = _text("bin/preflight.sh")
    block = preflight[preflight.index('if [ -n "${OP_CONTAINERD_ROOT:-}" ]; then') :]
    block = block[: block.index("\nfi\n")]
    assert "containerd config dump" in block
    assert "^root = " in block
    assert '[ "${containerd_root}" = "${OP_CONTAINERD_ROOT}" ]' in block
    assert "|| bad " in block


def _preflight_root_check(tmp_path: Path, dump: str) -> subprocess.CompletedProcess[str]:
    """The preflight's containerd root block, run as written, with `sudo` stubbed to print
    ``dump`` for `containerd config dump` (review HIGH-1)."""
    preflight = _text("bin/preflight.sh")
    start = preflight.index('if [ -n "${OP_CONTAINERD_ROOT:-}" ]; then')
    block = preflight[start : preflight.index("\nfi\n", start) + 4]
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    (stubs / "dump.out").write_text(dump, encoding="utf-8", newline="\n")
    (stubs / "sudo").write_text(
        '#!/usr/bin/env bash\ncat "${STUB_DIR}/dump.out"\n', encoding="utf-8", newline="\n"
    )
    (stubs / "sudo").chmod(0o755)
    script = tmp_path / "check.sh"
    script.write_text(
        "set -u\nfails=0\n"
        'ok() { echo "OK $*"; }\nbad() { echo "BAD $*"; fails=$((fails + 1)); }\n'
        f'export STUB_DIR="{stubs.as_posix()}"\n'
        f'PATH="$(cd "{stubs.as_posix()}" && pwd):$PATH"\n'
        "OP_CONTAINERD_ROOT=/srv/containerd\n" + block,
        encoding="utf-8",
        newline="\n",
    )
    return _bash(f'bash "{script.as_posix()}"')


CONTAINERD_DUMP = "version = 3\n{root}\nstate = '/run/containerd'\n\n[grpc]\n  address = 'x'\n"


@needs_bash
@pytest.mark.parametrize(
    "line",
    ["root = '/srv/containerd'", 'root = "/srv/containerd"'],
    ids=["containerd 2 single quotes", "containerd 1 double quotes"],
)
def test_preflight_reads_containerds_root_in_either_quote_style(tmp_path: Path, line: str) -> None:
    """Review HIGH-1: containerd 2.4.2 on the Oracle host prints single quotes."""
    result = _preflight_root_check(tmp_path, CONTAINERD_DUMP.format(root=line))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "OK containerd's root is /srv/containerd"


@needs_bash
@pytest.mark.parametrize(
    "dump",
    ["", CONTAINERD_DUMP.format(root="root = '/var/lib/containerd'")],
    ids=["empty dump", "another root"],
)
def test_preflight_fails_an_empty_or_other_root(tmp_path: Path, dump: str) -> None:
    result = _preflight_root_check(tmp_path, dump)
    assert result.stdout.startswith("BAD containerd's root is"), result.stdout


@needs_bash
@pytest.mark.parametrize(
    ("failing", "stuck", "calls"),
    [
        # Round 3 LOW-4: the drop-in's directory; on the live host's state the config's own
        # `install -d` of the same directory never runs, so only the drop-in's can fail here.
        ((("install", "/etc"),), False, []),
        ((), True, []),  # the drop-in's printf
        ((("chmod", "dropin.conf.tmp"),), False, []),  # round 3 LOW-4
        ((("mv", "dropin.conf.tmp"),), False, []),  # the drop-in's mv
        ((("systemctl", "daemon-reload"),), False, []),
    ],
    ids=["drop-in install", "drop-in printf", "drop-in chmod", "drop-in mv", "daemon-reload"],
)
def test_a_failing_drop_in_step_fails_the_whole(
    tmp_path: Path,
    failing: tuple[tuple[str, str], ...],
    stuck: bool,  # noqa: FBT001
    calls: list[str],
) -> None:
    """Round 2 LOW-2: the drop-in's own steps, on the live host's state (root already set)."""
    root = (tmp_path / "srv-containerd").as_posix()
    config = MOVED.replace("/srv/containerd", root)
    result, systemctl = _apply(
        tmp_path,
        config=config,
        images="sha256:abc\n",
        dropin=None,
        failing=failing,
        stuck_dropin_tmp=stuck,
    )
    assert result.returncode == 1
    assert systemctl == calls


@needs_bash
def test_a_failing_restart_fails_the_whole(tmp_path: Path) -> None:
    result, systemctl = _apply(
        tmp_path,
        config=CONTAINERD_DEFAULT,
        images="",
        dropin=DROPIN,
        failing=(("systemctl", "restart"),),
    )
    assert result.returncode == 1
    assert systemctl == ["daemon-reload"]


@needs_bash
def test_an_existing_backup_survives_a_rewrite(tmp_path: Path) -> None:
    """Round 2 LOW-2: the first backup is the package's config; it is never overwritten."""
    result, _ = _apply(
        tmp_path, config=CONTAINERD_DEFAULT, images="", dropin=None, bak="the first backup\n"
    )
    assert result.returncode == 0, result.stderr
    bak = tmp_path / "etc" / "config.toml.bak-r9289"
    assert bak.read_text(encoding="utf-8") == "the first backup\n"
