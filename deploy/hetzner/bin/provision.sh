#!/usr/bin/env bash
# Open Persona: provision and harden the Hetzner box. Idempotent: run it again any time.
#
# Run as root from the checkout:  sudo deploy/hetzner/bin/provision.sh
#
# Re-asserts the cloud-init.yaml baseline (key-only SSH for `deploy`, no root login, ufw
# default deny, fail2ban, unattended security upgrades without automatic reboots), then:
#   * Docker Engine + the compose plugin from Docker's own apt repository
#   * ufw rules for the app: 80/tcp, 443/tcp (Caddy), 7881/tcp + 7882/udp (LiveKit media)
#   * Docker log rotation defaults
#   * the directory layout (below), owned by `deploy` (uid 1000, the same uid as the
#     images' `persona` user, so the bind mounts need no remapping)
#   * the nightly backup timer
#
#   /opt/open-persona/repo     the git checkout (bin/deploy.sh builds from it)
#   /opt/open-persona/env      the env files, mode 0700 dir / 0600 files, OUTSIDE the checkout
#   /srv/open-persona/persona  the api data (audit, workspace, tools, mirrors, HF cache)
#   /srv/open-persona/voice    voice's HF cache
#   /srv/open-persona/restore  the uploaded Fly backup set, during the restore only
#   /srv/open-persona/backups  pre-deploy dumps and nightly backups
#
# What it does NOT do: open 7880 (LiveKit signaling goes through Caddy), 5432, 8000 or
# 8001. Docker-published ports would bypass ufw, which is why the compose file publishes
# nothing on a public address.

set -euo pipefail

[ "$(id -u)" = 0 ] || { echo "provision: run as root (sudo)" >&2; exit 1; }
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KIT_DIR="$(dirname "${SCRIPT_DIR}")"
OP_USER="deploy"
say() { echo "provision: $*"; }
# Writes stdin to a root-owned 0644 config file.
write_file() { install -d -m 0755 "$(dirname "$1")"; cat > "$1"; chmod 0644 "$1"; }

. /etc/os-release
[ "${ID}" = ubuntu ] && [ "${VERSION_ID}" = "24.04" ] \
  || say "WARNING: written for Ubuntu 24.04, found ${ID} ${VERSION_ID}"
[ "$(uname -m)" = x86_64 ] || { echo "provision: x86_64 only (silero-vad-lite has no arm64 wheel)" >&2; exit 1; }

export DEBIAN_FRONTEND=noninteractive

# --- the operator user ------------------------------------------------------------------
getent group docker >/dev/null || groupadd docker
if ! id "${OP_USER}" >/dev/null 2>&1; then
  useradd --uid 1000 --create-home --shell /bin/bash --groups sudo,docker "${OP_USER}"
  passwd -l "${OP_USER}" >/dev/null
  say "created ${OP_USER}; add its SSH key to /home/${OP_USER}/.ssh/authorized_keys BEFORE logging out"
fi
usermod -aG docker,sudo "${OP_USER}"
[ "$(id -u "${OP_USER}")" = 1000 ] \
  || say "WARNING: ${OP_USER} is not uid 1000; chown the data dirs to 1000 instead of ${OP_USER}"

# --- baseline packages and SSH ----------------------------------------------------------
apt-get update -q
apt-get install -y -q ufw fail2ban unattended-upgrades ca-certificates curl git jq gnupg

# Only with a real key in place: written without one, the next sshd restart (an openssh
# security update, a reboot) would lock everyone out. A key line starts with its type.
if grep -qE '^(ssh-(ed25519|rsa)|ecdsa-sha2-|sk-)' "/home/${OP_USER}/.ssh/authorized_keys" 2>/dev/null; then
  write_file /etc/ssh/sshd_config.d/10-open-persona.conf <<'EOF'
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
AllowUsers deploy
EOF
  sshd -t && systemctl reload ssh
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

write_file /etc/apt/apt.conf.d/52open-persona-unattended <<'EOF'
// Security updates install by themselves; the box never reboots by itself (calls drop).
Unattended-Upgrade::Automatic-Reboot "false";
Unattended-Upgrade::Remove-Unused-Dependencies "true";
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
  "live-restore": true
}
EOF
systemctl enable docker
systemctl restart docker

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
for d in persona voice backups restore; do
  install -d -o 1000 -g 1000 -m 0750 "/srv/open-persona/${d}"
done

# --- nightly backup timer ---------------------------------------------------------------
install -m 0644 "${KIT_DIR}/systemd/open-persona-backup.service" /etc/systemd/system/
install -m 0644 "${KIT_DIR}/systemd/open-persona-backup.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now open-persona-backup.timer

say "done. $(docker --version); $(docker compose version)"
say "ufw:"; ufw status verbose | sed 's/^/provision:   /'
[ -f /var/run/reboot-required ] && say "a reboot is required (kernel or libc update); reboot now, before any traffic"
exit 0
