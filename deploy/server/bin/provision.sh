#!/usr/bin/env bash
# Open Persona: provision and harden a host for the server kit. Idempotent: run it again
# any time.
#
#   sudo bin/provision.sh --profile oracle [--public-ipv4 <address>]
#   sudo bin/provision.sh --only-operator-user
#
# THE CORE HOST (owner ruling 2026-11-07) is an existing Oracle Cloud Ubuntu 24.04 arm64
# server, reached as `ubuntu` with passwordless sudo. From the checkout, as ubuntu:
#     sudo /opt/open-persona/repo/deploy/server/bin/provision.sh --profile oracle \
#          --public-ipv4 <the instance's public IPv4>
# It creates the operator `deploy` (uid 2000) with the INVOKING user's SSH key(s), and
# keeps `ubuntu` (the owner's login, whose home holds the owner's other work) in the SSH
# allow list. On a fresh box reached as root the key comes from root instead.
# cloud-init.yaml is the alternative first-boot path for a NEW host.
#
# Everything host-specific comes from the host profile (hosts/<profile>.env, loaded by
# lib/profile.sh): the architecture, the SSH allow list, the data uid, the block volume,
# swap, Oracle's firewall and the units to mask. The name is recorded in
# /etc/open-persona/host-profile, which every later script reads.
#
# What it sets up, in order:
#   * the operator user, key-only SSH (no root login, only the profile's users), fail2ban,
#     unattended security upgrades without automatic reboots
#   * the block volume: formatted ext4 ONLY when it carries no signature at all, mounted
#     by UUID (nofail) at the profile's mount; nothing else runs without it
#   * swap on the block volume, a journald size cap
#   * Oracle's image firewall handed to ufw (lib/oracle_firewall.sh), then ufw default
#     deny with 22 (limited), 80 and 443 only; LiveKit's ports belong to the voice host
#   * Docker Engine + compose from Docker's apt repository for this architecture (signing
#     key checked against the published fingerprint, Docker in unattended upgrades), its
#     data root on the block volume, and docker.service ordered after that mount
#   * /etc/docker/daemon.json: log rotation, live-restore, no-new-privileges everywhere
#   * the link-local range (169.254.0.0/16: metadata, iSCSI) dropped for bridged containers
#   * the profile's units masked (rpcbind, the unified-monitoring-agent)
#   * the directory layout (below), the nightly backup, the daily disk check, the
#     failure alert and the login banner
#
#   /opt/open-persona/repo     the git checkout (bin/deploy.sh builds from it)
#   /opt/open-persona/env      the env files, mode 0700 dir / 0600 files, OUTSIDE the checkout
#   /srv/open-persona/persona  the api data, owned by the profile's data uid (2001 on Oracle)
#   /srv/open-persona/restore  the uploaded backup set, during the restore only (deploy)
#   /srv/open-persona/backups  pre-deploy dumps (deploy) and nightly backups (root)
#   /var/lib/open-persona/alerts  the alert marker bin/alert.sh writes
#
# `deploy` is uid 2000 and the data uid is 2001: neither the operator nor `ubuntu` (uid
# 1000) owns the app's data. `deploy` is still root-equivalent (sudo and the docker group),
# so its SSH key is the key to the host (RUNBOOK.md, "Who can do what").
#
# What it does NOT do: open 5432 or 8000. Docker-published ports would bypass ufw, which is
# why the compose file publishes nothing on a public address.

set -euo pipefail

[ "$(id -u)" = 0 ] || { echo "provision: run as root (sudo)" >&2; exit 1; }
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KIT_DIR="$(dirname "${SCRIPT_DIR}")"
OP_USER="deploy"
OP_UID=2000
# Docker's published release-signing key (docs.docker.com/engine/install/ubuntu).
DOCKER_KEY_FPR="9DC858229FC7DD38854AE2D88D81803C0EBFCD88"
say() { echo "provision: $*"; }
die() { echo "provision: FAILED: $*" >&2; exit 1; }
# Writes stdin to a root-owned 0644 config file.
write_file() { install -d -m 0755 "$(dirname "$1")"; cat > "$1"; chmod 0644 "$1"; }

ONLY_OPERATOR=0
PUBLIC_IPV4=""
while [ $# -gt 0 ]; do
  case "$1" in
    --profile) OP_HOST_PROFILE="$2"; export OP_HOST_PROFILE; shift 2 ;;
    --public-ipv4) PUBLIC_IPV4="$2"; shift 2 ;;
    --only-operator-user) ONLY_OPERATOR=1; shift ;;
    *) die "unknown argument: $1" ;;
  esac
done
# shellcheck source=lib/profile.sh
. "${SCRIPT_DIR}/lib/profile.sh"
# shellcheck source=lib/oracle_firewall.sh
. "${SCRIPT_DIR}/lib/oracle_firewall.sh"
: "${OP_DATA_UID:?the host profile sets no OP_DATA_UID}"
: "${OP_EXPECT_ARCH:?the host profile sets no OP_EXPECT_ARCH}"
: "${OP_SSH_ALLOW_USERS:?the host profile sets no OP_SSH_ALLOW_USERS}"
OP_DOCKER_DATA_ROOT="${OP_DOCKER_DATA_ROOT:-/var/lib/docker}"

# The SSH keys `deploy` starts with: the invoking user's when run through sudo (Oracle:
# ubuntu, whose key the owner already uses), root's on a box reached as root. Overridable
# only for a test.
if [ -n "${SUDO_USER:-}" ] && [ "${SUDO_USER}" != root ]; then
  default_keys="$(getent passwd "${SUDO_USER}" | cut -d: -f6)/.ssh/authorized_keys"
else
  default_keys=/root/.ssh/authorized_keys
fi
ROOT_KEYS="${ROOT_KEYS:-${default_keys}}"
KEY_LINE='^(ssh-(ed25519|rsa)|ecdsa-sha2-|sk-)'

# --- the operator user ------------------------------------------------------------------
# Creates `deploy` (uid 2000, sudo without password, docker group, password locked) and,
# when it has no key of its own yet, gives it the starting keys above. Idempotent: an
# existing deploy user keeps its own keys.
setup_operator_user() {
  getent group docker >/dev/null || groupadd --system docker  # system gid, never a user's
  if ! id "${OP_USER}" >/dev/null 2>&1; then
    useradd --uid "${OP_UID}" --create-home --shell /bin/bash --groups sudo,docker "${OP_USER}"
    say "created ${OP_USER} (uid ${OP_UID})"
  fi
  usermod -aG docker,sudo "${OP_USER}"
  passwd -l "${OP_USER}" >/dev/null 2>&1 || say "WARNING: could not lock ${OP_USER}'s password"
  [ "$(id -u "${OP_USER}")" != "${OP_DATA_UID}" ] \
    || say "WARNING: ${OP_USER} is uid ${OP_DATA_UID}, the app's data owner; recreate it as uid ${OP_UID}"
  install -d -m 0755 /etc/sudoers.d
  printf '%s ALL=(ALL) NOPASSWD:ALL\n' "${OP_USER}" > "/etc/sudoers.d/90-${OP_USER}"
  chmod 0440 "/etc/sudoers.d/90-${OP_USER}"

  local home keys
  home="$(getent passwd "${OP_USER}" | cut -d: -f6)"
  keys="${home}/.ssh/authorized_keys"
  install -d -o "${OP_USER}" -g "${OP_USER}" -m 0700 "${home}/.ssh"
  if ! grep -qE "${KEY_LINE}" "${keys}" 2>/dev/null; then
    if grep -qE "${KEY_LINE}" "${ROOT_KEYS}" 2>/dev/null; then
      grep -E "${KEY_LINE}" "${ROOT_KEYS}" > "${keys}"
      say "gave ${OP_USER} the key(s) of ${ROOT_KEYS}: $(wc -l < "${keys}") key(s)"
    else
      say "WARNING: neither ${OP_USER} nor ${ROOT_KEYS} has an SSH key; the SSH lockdown will NOT be applied"
    fi
  fi
  if [ -f "${keys}" ]; then
    chown "${OP_USER}:${OP_USER}" "${keys}"
    chmod 0600 "${keys}"
  fi
  # An account already holding the data uid would own the app's data on the host.
  local holder
  holder="$(getent passwd "${OP_DATA_UID}" | cut -d: -f1 || true)"
  [ -z "${holder}" ] || say "WARNING: uid ${OP_DATA_UID} (the data uid) belongs to '${holder}'; it would own the app's data"
}

if [ "${ONLY_OPERATOR}" = 1 ]; then
  setup_operator_user
  exit 0
fi

. /etc/os-release
[ "${ID}" = ubuntu ] && [ "${VERSION_ID}" = "24.04" ] \
  || say "WARNING: written for Ubuntu 24.04, found ${ID} ${VERSION_ID}"
[ "$(uname -m)" = "${OP_EXPECT_ARCH}" ] \
  || die "profile ${OP_HOST_PROFILE} expects ${OP_EXPECT_ARCH}, this host is $(uname -m)"

# --- the profile and the public address, for every later script --------------------------
install -d -m 0755 /etc/open-persona
printf '%s\n' "${OP_HOST_PROFILE}" > /etc/open-persona/host-profile
if [ -n "${PUBLIC_IPV4}" ]; then
  printf '%s' "${PUBLIC_IPV4}" | grep -Eq '^[0-9]{1,3}(\.[0-9]{1,3}){3}$' \
    || die "--public-ipv4 '${PUBLIC_IPV4}' is not an IPv4 address"
  case "${PUBLIC_IPV4}" in
    10.* | 192.168.* | 172.1[6-9].* | 172.2[0-9].* | 172.3[0-1].* | 127.* | 169.254.*)
      die "--public-ipv4 ${PUBLIC_IPV4} is a private address; give the public one" ;;
  esac
  # On OCI the public address is NAT'd to a private one on the interface, so preflight
  # cannot learn it from the routing table: it reads it from here.
  printf '%s\n' "${PUBLIC_IPV4}" > /etc/open-persona/public-ipv4
fi
chmod 0644 /etc/open-persona/*

export DEBIAN_FRONTEND=noninteractive

setup_operator_user

# --- Oracle's firewall, kept before apt touches it ------------------------------------------
# Installing ufw REMOVES iptables-persistent and netfilter-persistent on Oracle's image
# (seen on the host, 2026-11-07). So, before apt runs:
#   1. a copy of the image's rules, taken once and kept;
#   2. the InstanceServices chain read from that copy and saved beside it, so every later
#      run (and this one, after apt) merges the same rules whatever apt removed;
#   3. SSH held open by an explicit ACCEPT at the top of INPUT for the length of the
#      install, whatever the package scripts do to the live rules. It is removed once ufw
#      (which limits 22) owns INPUT.
FW_BACKUP=/root/open-persona-firewall-backup
instance_rules=""
if [ "${OP_ORACLE_FIREWALL:-0}" = 1 ]; then
  install -d -m 0700 "${FW_BACKUP}"
  for f in /etc/iptables/rules.v4 /etc/iptables/rules.v6; do
    if [ -f "${f}" ] && [ ! -f "${FW_BACKUP}/$(basename "${f}")" ]; then
      cp -p "${f}" "${FW_BACKUP}/"
      say "kept Oracle's ${f} in ${FW_BACKUP}"
    fi
  done
  source_rules="${FW_BACKUP}/rules.v4"
  [ -f "${source_rules}" ] || source_rules=/etc/iptables/rules.v4
  instance_rules="$(instance_services_rules "${source_rules}")"
  if [ -n "${instance_rules}" ]; then
    printf '%s\n' "${instance_rules}" > "${FW_BACKUP}/instance-services.rules"
    chmod 0600 "${FW_BACKUP}/instance-services.rules"
    say "InstanceServices: $(printf '%s\n' "${instance_rules}" | wc -l) rule(s) kept"
  elif [ -s "${FW_BACKUP}/instance-services.rules" ]; then
    instance_rules="$(cat "${FW_BACKUP}/instance-services.rules")"
  else
    say "WARNING: no InstanceServices chain in ${source_rules}; preflight will report it"
  fi
  iptables -C INPUT -p tcp --dport 22 -m comment --comment open-persona-ssh-hold -j ACCEPT 2>/dev/null \
    || iptables -I INPUT 1 -p tcp --dport 22 -m comment --comment open-persona-ssh-hold -j ACCEPT
fi

# --- baseline packages and SSH ----------------------------------------------------------
apt-get update -q
apt-get install -y -q ufw fail2ban unattended-upgrades ca-certificates curl git jq gnupg

# The allow list, from the profile (comma separated). It must hold the operator; the
# invoking user is warned about if it is left out (its session stays, its next login not).
allow_users="${OP_SSH_ALLOW_USERS//,/ }"
for u in ${allow_users}; do
  printf '%s' "${u}" | grep -Eq '^[a-z_][a-z0-9_-]*$' || die "OP_SSH_ALLOW_USERS: bad user name '${u}'"
  [ "${u}" != root ] || die "OP_SSH_ALLOW_USERS must not admit root"
done
case " ${allow_users} " in
  *" ${OP_USER} "*) ;;
  *) die "OP_SSH_ALLOW_USERS must include ${OP_USER}" ;;
esac
if [ -n "${SUDO_USER:-}" ]; then
  case " ${allow_users} " in
    *" ${SUDO_USER} "*) ;;
    *) say "WARNING: ${SUDO_USER} (running this) is not in OP_SSH_ALLOW_USERS: its next SSH login will be refused" ;;
  esac
fi

# Only with a real key in place: written without one, the next sshd restart (an openssh
# security update, a reboot) would lock everyone out. A key line starts with its type.
# Check `ssh deploy@<ip>` from a second terminal before closing this session.
if grep -qE "${KEY_LINE}" "$(getent passwd "${OP_USER}" | cut -d: -f6)/.ssh/authorized_keys" 2>/dev/null; then
  write_file /etc/ssh/sshd_config.d/10-open-persona.conf <<EOF
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
AllowUsers ${allow_users}
EOF
  # A config sshd rejects must never be reloaded into: stop here instead.
  sshd -t || die "sshd rejects its configuration; nothing was reloaded"
  systemctl reload ssh
else
  rm -f /etc/ssh/sshd_config.d/10-open-persona.conf
  say "WARNING: ${OP_USER} has no SSH key; the SSH lockdown was NOT applied. Add the key, then run this again."
fi

write_file /etc/fail2ban/jail.d/sshd.local <<'EOF'
[sshd]
enabled = true
backend = systemd
maxretry = 5
findtime = 10m
bantime = 1h
EOF
systemctl enable --now fail2ban
systemctl restart fail2ban

# Docker's repository joins the upgrade origins: its security fixes install like
# Ubuntu's, and live-restore (below) keeps the containers running across the restart.
write_file /etc/apt/apt.conf.d/52open-persona-unattended <<'EOF'
// Security updates install by themselves; the host never reboots by itself.
Unattended-Upgrade::Automatic-Reboot "false";
Unattended-Upgrade::Remove-Unused-Dependencies "true";
Unattended-Upgrade::Origins-Pattern {
        "origin=Docker,label=Docker CE,codename=${distro_codename}";
};
EOF
write_file /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
systemctl enable --now unattended-upgrades

# --- the block volume ---------------------------------------------------------------------
# Formatted only when it carries NO signature at all (blkid -p finds nothing and it has no
# partitions); a device holding anything but our own ext4 `op-data` is refused, never
# formatted. Mounted by UUID with nofail, so a missing volume cannot stop the boot; every
# data-writing script then refuses to run (lib/profile.sh require_data_mount) and Docker
# does not start (RequiresMountsFor, below).
setup_data_volume() {
  [ -n "${OP_DATA_MOUNT:-}" ] || return 0
  if mountpoint -q "${OP_DATA_MOUNT}"; then
    say "block volume: ${OP_DATA_MOUNT} already mounted"
    return 0
  fi
  [ -n "${OP_DATA_DEVICE:-}" ] || die "the profile sets OP_DATA_MOUNT but no OP_DATA_DEVICE"
  local dev root_disk fstype label uuid
  dev="$(readlink -f "${OP_DATA_DEVICE}")"
  [ -b "${dev}" ] || die "${OP_DATA_DEVICE} is not attached (OCI console: attach the block volume, paravirtualized)"
  root_disk="$(lsblk -no PKNAME "$(findmnt -no SOURCE /)" 2>/dev/null || true)"
  [ "$(basename "${dev}")" != "${root_disk}" ] || die "${OP_DATA_DEVICE} is the boot disk"
  ! findmnt -rn -S "${dev}" >/dev/null || die "${OP_DATA_DEVICE} is already mounted elsewhere"
  if blkid -p "${dev}" >/dev/null 2>&1; then
    fstype="$(blkid -p -o value -s TYPE "${dev}" 2>/dev/null || true)"
    label="$(blkid -p -o value -s LABEL "${dev}" 2>/dev/null || true)"
    [ "${fstype}" = ext4 ] && [ "${label}" = op-data ] \
      || die "${OP_DATA_DEVICE} already holds '${fstype:-a partition table}' (label '${label}'); refusing to format or mount it"
  else
    [ -z "$(lsblk -nro NAME "${dev}" | tail -n +2)" ] || die "${OP_DATA_DEVICE} has partitions; refusing to format it"
    say "formatting the blank ${OP_DATA_DEVICE} as ext4 (label op-data)"
    mkfs.ext4 -q -L op-data -m 1 "${dev}"
  fi
  uuid="$(blkid -p -o value -s UUID "${dev}")"
  [ -n "${uuid}" ] || die "${OP_DATA_DEVICE} has no filesystem UUID"
  install -d -m 0755 "${OP_DATA_MOUNT}"
  [ -z "$(ls -A "${OP_DATA_MOUNT}")" ] \
    || die "${OP_DATA_MOUNT} on the boot disk is not empty; the mount would hide it. Move it aside first"
  grep -q "^UUID=${uuid} " /etc/fstab \
    || printf 'UUID=%s %s ext4 defaults,noatime,nofail 0 2\n' "${uuid}" "${OP_DATA_MOUNT}" >> /etc/fstab
  systemctl daemon-reload
  mount "${OP_DATA_MOUNT}"
  mountpoint -q "${OP_DATA_MOUNT}" || die "mounting ${OP_DATA_MOUNT} failed"
  say "block volume: ${OP_DATA_DEVICE} mounted at ${OP_DATA_MOUNT} (UUID ${uuid})"
}
setup_data_volume

# --- swap and the journal -------------------------------------------------------------------
# A cushion against an out-of-memory kill on a 12 GB host, on the block volume. Postgres
# never swaps (its memswap_limit equals its limit); swappiness keeps the rest in RAM.
if [ "${OP_SWAP_GB:-0}" -gt 0 ]; then
  swapfile="${OP_DATA_MOUNT:-}/swapfile"
  if ! swapon --show=NAME --noheadings | grep -qx "${swapfile}"; then
    if [ ! -f "${swapfile}" ]; then
      fallocate -l "${OP_SWAP_GB}G" "${swapfile}"
      chmod 0600 "${swapfile}"
      mkswap "${swapfile}" >/dev/null
    fi
    chmod 0600 "${swapfile}"
    grep -q "^${swapfile} " /etc/fstab || printf '%s none swap sw,nofail 0 0\n' "${swapfile}" >> /etc/fstab
    swapon "${swapfile}"
    say "swap: ${OP_SWAP_GB} GB at ${swapfile}"
  fi
  write_file /etc/sysctl.d/60-open-persona.conf <<'EOF'
vm.swappiness=10
EOF
  sysctl -q -p /etc/sysctl.d/60-open-persona.conf
fi
write_file /etc/systemd/journald.conf.d/60-open-persona.conf <<'EOF'
[Journal]
SystemMaxUse=500M
EOF
systemctl restart systemd-journald

# --- firewall -------------------------------------------------------------------------------
# Oracle's image: ufw takes INPUT over and keeps the InstanceServices OUTPUT chain (read
# before apt ran, above) in /etc/ufw/before.rules. Installing ufw normally REMOVED
# netfilter-persistent; if it is still installed, it is masked, not stopped (stopping can
# flush the live rules mid-session). The image's rule files are moved aside either way, so
# nothing restores Oracle's INPUT and FORWARD REJECTs at the next boot.
if [ "${OP_ORACLE_FIREWALL:-0}" = 1 ]; then
  [ -f "${FW_BACKUP}/before.rules" ] || cp -p /etc/ufw/before.rules "${FW_BACKUP}/before.rules"
  merge_before_rules /etc/ufw/before.rules "${instance_rules}" > /etc/ufw/before.rules.new
  chmod 0640 /etc/ufw/before.rules.new
  mv /etc/ufw/before.rules.new /etc/ufw/before.rules
  if systemctl list-unit-files --no-legend netfilter-persistent.service | grep -q .; then
    systemctl disable netfilter-persistent >/dev/null 2>&1 || say "WARNING: could not disable netfilter-persistent"
    systemctl mask netfilter-persistent >/dev/null
  else
    say "netfilter-persistent is not installed (the ufw install removed it): nothing to mask"
  fi
  for f in rules.v4 rules.v6; do
    [ ! -f "/etc/iptables/${f}" ] || mv "/etc/iptables/${f}" "/etc/iptables/${f}.oracle-disabled"
  done
fi

ufw default deny incoming
ufw default allow outgoing
ufw limit 22/tcp comment 'ssh'
ufw allow 80/tcp comment 'caddy: http, ACME challenge, redirect'
ufw allow 443/tcp comment 'caddy: https'
ufw --force enable
ufw reload

if [ "${OP_ORACLE_FIREWALL:-0}" = 1 ]; then
  # The image's REJECTs are still loaded in the running kernel, ahead of ufw's chains:
  # remove them now rather than waiting for a reboot. SSH stays open throughout (the hold,
  # Oracle's own rule above the REJECT, and now ufw).
  for chain in INPUT FORWARD; do
    while iptables -C "${chain}" -j REJECT --reject-with icmp-host-prohibited 2>/dev/null; do
      iptables -D "${chain}" -j REJECT --reject-with icmp-host-prohibited
    done
    while ip6tables -C "${chain}" -j REJECT --reject-with icmp6-adm-prohibited 2>/dev/null; do
      ip6tables -D "${chain}" -j REJECT --reject-with icmp6-adm-prohibited
    done
  done
  # ufw owns INPUT and allows (limits) 22: the hold taken before apt goes.
  while iptables -C INPUT -p tcp --dport 22 -m comment --comment open-persona-ssh-hold -j ACCEPT 2>/dev/null; do
    iptables -D INPUT -p tcp --dport 22 -m comment --comment open-persona-ssh-hold -j ACCEPT
  done
fi

# --- units the host does not need -----------------------------------------------------------
# Masked by name prefix from the profile. oracle-cloud-agent is refused here even if a
# profile names it: Oracle's idle-instance check reads the metrics it reports.
for prefix in ${OP_DISABLE_UNIT_PREFIXES//,/ }; do
  case "${prefix}" in oracle-cloud-agent*) die "refusing to mask ${prefix}" ;; esac
  for unit in $(systemctl list-unit-files --no-legend "${prefix}*" | awk '{print $1}'); do
    # disable may fail for a static unit; the mask is what holds, and it is not tolerated.
    systemctl disable --now "${unit}" >/dev/null 2>&1 || true
    systemctl mask "${unit}" >/dev/null
    say "masked ${unit}"
  done
done

# --- Docker Engine + compose plugin (Docker's apt repository) -----------------------------
if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  fpr="$(gpg --show-keys --with-colons /etc/apt/keyrings/docker.asc | awk -F: '/^fpr/ {print $10; exit}')"
  if [ "${fpr}" != "${DOCKER_KEY_FPR}" ]; then
    rm -f /etc/apt/keyrings/docker.asc
    die "Docker's signing key fingerprint is '${fpr}', expected ${DOCKER_KEY_FPR}; refusing it"
  fi
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -q
  apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
# Moving the data root under existing containers or images would orphan them silently.
current_root="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || true)"
if [ -n "${current_root}" ] && [ "${current_root}" != "${OP_DOCKER_DATA_ROOT}" ] \
    && [ -n "$(docker ps -aq)$(docker images -q)" ]; then
  die "Docker holds containers or images under ${current_root}; move them before switching to ${OP_DOCKER_DATA_ROOT}"
fi
install -d -m 0710 "${OP_DOCKER_DATA_ROOT}"
write_file /etc/docker/daemon.json <<EOF
{
  "data-root": "${OP_DOCKER_DATA_ROOT}",
  "log-driver": "json-file",
  "log-opts": { "max-size": "50m", "max-file": "5" },
  "live-restore": true,
  "no-new-privileges": true
}
EOF
if [ -n "${OP_DATA_MOUNT:-}" ]; then
  # Docker never starts without the block volume: an empty data root on the boot disk
  # would come up with no database and no images.
  write_file /etc/systemd/system/docker.service.d/10-open-persona.conf <<EOF
[Unit]
RequiresMountsFor=${OP_DATA_MOUNT}
EOF
fi
systemctl daemon-reload
systemctl enable docker
systemctl restart docker

# --- the link-local range, out of reach of bridged containers ------------------------------
# The cloud serves instance metadata (including user data) and, on Oracle, iSCSI and other
# instance services on 169.254.0.0/16. No container needs any of it. Host-network
# containers (caddy) are not covered by DOCKER-USER; they hold nothing worth trading for it.
install -m 0644 "${KIT_DIR}/systemd/open-persona-metadata-block.service" /etc/systemd/system/

# --- directories ------------------------------------------------------------------------
install -d -o "${OP_USER}" -g "${OP_USER}" -m 0755 /opt/open-persona /srv/open-persona
install -d -o "${OP_USER}" -g "${OP_USER}" -m 0700 /opt/open-persona/env
# The checkout was cloned before this ran; deploy builds and pulls from it.
if [ -d /opt/open-persona/repo ]; then
  chown -R "${OP_USER}:${OP_USER}" /opt/open-persona/repo
fi
install -d -o "${OP_DATA_UID}" -g "${OP_DATA_UID}" -m 0750 /srv/open-persona/persona
for d in backups restore; do
  install -d -o "${OP_USER}" -g "${OP_USER}" -m 0700 "/srv/open-persona/${d}"
done
install -d -o root -g "${OP_USER}" -m 2770 /var/lib/open-persona/alerts

# --- timers, the failure alert and the login banner -------------------------------------
for unit in open-persona-backup.service open-persona-backup.timer \
            open-persona-diskcheck.service open-persona-diskcheck.timer \
            open-persona-alert@.service; do
  install -m 0644 "${KIT_DIR}/systemd/${unit}" /etc/systemd/system/
done
install -m 0755 "${KIT_DIR}/systemd/99-open-persona-alerts" /etc/update-motd.d/
systemctl daemon-reload
systemctl enable --now open-persona-metadata-block.service
systemctl restart open-persona-metadata-block.service
systemctl enable --now open-persona-backup.timer open-persona-diskcheck.timer

say "done. profile ${OP_HOST_PROFILE}; $(docker --version); $(docker compose version)"
say "time synchronised: $(timedatectl show -p NTPSynchronized --value 2>/dev/null || echo unknown)"
say "ufw:"; ufw status verbose | sed 's/^/provision:   /'
[ ! -f /var/run/reboot-required ] || say "a reboot is required (kernel or libc update); reboot now, before any traffic"
[ "${OP_ORACLE_FIREWALL:-0}" != 1 ] || say "reboot once now and run bin/preflight.sh: it checks the firewall survived the boot"
exit 0
