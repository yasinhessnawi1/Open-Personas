#!/usr/bin/env bash
# Open Persona: keep bridged containers off the link-local range, DNS excepted. Run by
# open-persona-metadata-block.service whenever Docker (re)starts: the rules live in
# Docker's DOCKER-USER chain, which does not survive a reboot.
#
# The range 169.254.0.0/16 holds the cloud metadata service (user data, credentials) and,
# on Oracle, iSCSI and other instance services: no container needs any of it, EXCEPT DNS.
# On Oracle the host's resolver IS 169.254.169.254, and Docker's embedded DNS forwards to
# it from each container's own namespace, through DOCKER-USER. So, top to bottom:
#   udp 53 to 169.254.169.254 RETURN, tcp 53 to 169.254.169.254 RETURN, the /16 DROP.
#
# The new set is inserted FIRST and older copies below it are deleted after, so there is
# never a moment without the DROP. Nothing ever removes these rules (the unit has no
# ExecStop): the block holds while dockerd is stopped or restarting under live-restore.
# scripts/tests/test_server_kit_dns.py runs this against a fake iptables.

set -euo pipefail

iptables -I DOCKER-USER 1 -d 169.254.0.0/16 -j DROP
iptables -I DOCKER-USER 1 -d 169.254.169.254/32 -p tcp --dport 53 -j RETURN
iptables -I DOCKER-USER 1 -d 169.254.169.254/32 -p udp --dport 53 -j RETURN

# Older copies sit below the three just inserted, at rule 4 and after: every rule there
# whose destination is in the link-local range is ours. The destination is the SECOND
# address on a line (source first), however many option columns this iptables prints.
# Highest number first, so the numbers still to delete do not move.
stale="$(iptables -L DOCKER-USER -n --line-numbers | awk '
  $1 ~ /^[0-9]+$/ && $1 > 3 {
    seen = 0
    for (i = 2; i <= NF; i++) {
      if ($i ~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+(\/[0-9]+)?$/ && ++seen == 2) {
        if ($i ~ /^169\.254\./) print $1
        break
      }
    }
  }' | sort -rn)"
for rule in ${stale}; do
  iptables -D DOCKER-USER "${rule}"
done
