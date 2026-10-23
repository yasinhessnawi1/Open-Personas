#!/usr/bin/env bash
# Open Persona: provision and harden the Hetzner box. Idempotent: run it again any time.
#
# PRIMARY PATH (no cloud-init needed): the server is created with only the owner's SSH key
# on root. Log in as root once and run, from the checkout:
#     /opt/open-persona/repo/deploy/hetzner/bin/provision.sh
# It creates `deploy` (uid 2000) with root's key, then closes root login. Every later run
# is `sudo .../provision.sh` as deploy. cloud-init.yaml is the alternative path.
#
# Sets up the baseline (the deploy user, key-only SSH, no root login, ufw default deny,
# fail2ban, unattended security upgrades without automatic reboots), then:
#   * Docker Engine + the compose plugin from Docker's own apt repository, its signing key
#     checked against the published fingerprint, and Docker included in unattended upgrades
#   * /etc/docker/daemon.json: log rotation, live-restore (containers survive a daemon
#     upgrade), no-new-privileges for every container
#   * the cloud metadata address (169.254.169.254) dropped for bridged containers
#   * ufw rules for the app: 80/tcp, 443/tcp (Caddy), 7881/tcp + 7882/udp (LiveKit media)
#   * the directory layout (below)
#   * the nightly backup, the daily disk check, the failure alert and the login banner
#
#   /opt/open-persona/repo     the git checkout (bin/deploy.sh builds from it)
#   /opt/open-persona/env      the env files, mode 0700 dir / 0600 files, OUTSIDE the checkout
#   /srv/open-persona/persona  the api data, owned by uid 1000 (the images' `persona` user)
#   /srv/open-persona/voice    voice's HF cache, uid 1000
#   /srv/open-persona/restore  the uploaded Fly backup set, during the restore only (deploy)
#   /srv/open-persona/backups  pre-deploy dumps (deploy) and nightly backups (root)
#   /var/lib/open-persona/alerts  the alert marker bin/alert.sh writes
#
# `deploy` is uid 2000 on purpose, NOT 1000: the operator's login never owns the app's
# data. It is still root-equivalent (sudo and the docker group), so its SSH key is the
# key to the box (RUNBOOK.md, "Who can do what").
#
# What it does NOT do: open 7880 (LiveKit signaling goes through Caddy), 5432, 8000 or
# 8001. Docker-published ports would bypass ufw, which is why the compose file publishes
# nothing on a public address.

set -euo pipefail

[ "$(id -u)" = 0 ] || { echo "provision: run as root (sudo)" >&2; exit 1; }
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KIT_DIR="$(dirname "${SCRIPT_DIR}")"
OP_USER="deploy"
OP_UID=2000
DATA_UID=1000
# Docker's published release-signing key (docs.docker.com/engine/install/ubuntu).
DOCKER_KEY_FPR="9DC858229FC7DD38854AE2D88D81803C0EBFCD88"
say() { echo "provision: $*"; }
die() { echo "provision: FAILED: $*" >&2; exit 1; }
# Writes stdin to a root-owned 0644 config file.
write_file() { install -d -m 0755 "$(dirname "$1")"; cat > "$1"; chmod 0644 "$1"; }

# The SSH keys root was created with (the Hetzner console's "SSH key" field). The primary
# path is: log in as root with that key, run this script once, log in as deploy from then
# on. Overridable only for the container test.
ROOT_KEYS="${ROOT_KEYS:-/root/.ssh/authorized_keys}"
KEY_LINE='^(ssh-(ed25519|rsa)|ecdsa-sha2-|sk-)'

# --- the operator user ------------------------------------------------------------------
# Creates `deploy` (uid 2000, sudo without password, docker group, password locked) and,
# when it has no key of its own yet, gives it root's keys: the same key the owner chose in
# the console. Idempotent: an existing deploy user keeps its own keys.
setup_operator_user() {
  getent group docker >/dev/null || groupadd --system docker  # system gid, never 1000 (the data group)
  if ! id "${OP_USER}" >/dev/null 2>&1; then
    useradd --uid "${OP_UID}" --create-home --shell /bin/bash --groups sudo,docker "${OP_USER}"
    say "created ${OP_USER} (uid ${OP_UID})"
  fi
  usermod -aG docker,sudo "${OP_USER}"
  passwd -l "${OP_USER}" >/dev/null 2>&1 || true
  [ "$(id -u "${OP_USER}")" != "${DATA_UID}" ] \
    || say "WARNING: ${OP_USER} is uid ${DATA_UID}, the app's data owner; recreate it as uid ${OP_UID}"
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
      say "gave ${OP_USER} root's SSH key(s): $(wc -l < "${keys}") key(s)"
    else
      say "WARNING: neither ${OP_USER} nor root has an SSH key; the SSH lockdown will NOT be applied"
    fi
  fi
  if [ -f "${keys}" ]; then
    chown "${OP_USER}:${OP_USER}" "${keys}"
    chmod 0600 "${keys}"
  fi
}

if [ "${1:-}" = "--only-operator-user" ]; then
  setup_operator_user
  exit 0
fi

. /etc/os-release
[ "${ID}" = ubuntu ] && [ "${VERSION_ID}" = "24.04" ] \
  || say "WARNING: written for Ubuntu 24.04, found ${ID} ${VERSION_ID}"
[ "$(uname -m)" = x86_64 ] || die "x86_64 only (silero-vad-lite has no arm64 wheel)"

export DEBIAN_FRONTEND=noninteractive

setup_operator_user

# --- baseline packages and SSH ----------------------------------------------------------
apt-get update -q
apt-get install -y -q ufw fail2ban unattended-upgrades ca-certificates curl git jq gnupg

# Only with a real key in place: written without one, the next sshd restart (an openssh
# security update, a reboot) would lock everyone out. A key line starts with its type.
# Root login closes here: from now on the box is reached as deploy (the current root
# session stays open; check `ssh deploy@<ip>` from a second terminal before closing it).
if grep -qE "${KEY_LINE}" "$(getent passwd "${OP_USER}" | cut -d: -f6)/.ssh/authorized_keys" 2>/dev/null; then
  write_file /etc/ssh/sshd_config.d/10-open-persona.conf <<'EOF'
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
AllowUsers deploy
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
// Security updates install by themselves; the box never reboots by itself (calls drop).
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
  echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -q
  apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
write_file /etc/docker/daemon.json <<'EOF'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "50m", "max-file": "5" },
  "live-restore": true,
  "no-new-privileges": true
}
EOF
systemctl enable docker
systemctl restart docker

# --- the cloud metadata address, out of reach of bridged containers -----------------------
# Hetzner serves server metadata (including the cloud-init user data) on 169.254.169.254.
# No container needs it. Host-network containers (caddy, livekit) are not covered by
# DOCKER-USER; they hold nothing an attacker would trade for it.
install -m 0644 "${KIT_DIR}/systemd/open-persona-metadata-block.service" /etc/systemd/system/

# --- firewall ---------------------------------------------------------------------------
ufw default deny incoming
ufw default allow outgoing
ufw limit 22/tcp comment 'ssh'
ufw allow 80/tcp comment 'caddy: http, ACME challenge, redirect'
ufw allow 443/tcp comment 'caddy: https'
ufw allow 7881/tcp comment 'livekit: ICE over TCP fallback'
ufw allow 7882/udp comment 'livekit: ICE over UDP, single-port mux'
ufw --force enable

# --- directories ------------------------------------------------------------------------
install -d -o "${OP_USER}" -g "${OP_USER}" -m 0755 /opt/open-persona /srv/open-persona
install -d -o "${OP_USER}" -g "${OP_USER}" -m 0700 /opt/open-persona/env
# On the primary path root cloned the checkout; deploy builds and pulls from it.
if [ -d /opt/open-persona/repo ]; then
  chown -R "${OP_USER}:${OP_USER}" /opt/open-persona/repo
fi
for d in persona voice; do
  install -d -o "${DATA_UID}" -g "${DATA_UID}" -m 0750 "/srv/open-persona/${d}"
done
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
systemctl enable --now open-persona-backup.timer open-persona-diskcheck.timer

say "done. $(docker --version); $(docker compose version)"
say "ufw:"; ufw status verbose | sed 's/^/provision:   /'
[ -f /var/run/reboot-required ] && say "a reboot is required (kernel or libc update); reboot now, before any traffic"
exit 0
