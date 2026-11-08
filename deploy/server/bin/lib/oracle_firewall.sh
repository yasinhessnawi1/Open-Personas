# shellcheck shell=bash
# Open Persona: hand Oracle's image firewall to ufw. Sourced by provision.sh, never run.
#
# Oracle's Ubuntu images load /etc/iptables/rules.v4 through netfilter-persistent. Its
# INPUT chain REJECTs every port but 22 ahead of anything ufw adds, so a port ufw allows
# stays closed, and its FORWARD REJECT plus a netfilter-persistent reload would wipe
# Docker's chains. ufw takes INPUT over. One part of Oracle's file must survive: the
# `InstanceServices` OUTPUT chain, which keeps non-root processes off the iSCSI and
# metadata endpoints on 169.254.0.0/16 while letting the system reach NTP, DNS and the
# metadata service. Its rules are copied verbatim from the host's own file (never retyped
# here), into ufw's before.rules, where ufw restores them on every boot.

OP_FW_BEGIN_CHAIN="# BEGIN open-persona InstanceServices chain"
OP_FW_END_CHAIN="# END open-persona InstanceServices chain"
OP_FW_BEGIN_RULES="# BEGIN open-persona InstanceServices rules"
OP_FW_END_RULES="# END open-persona InstanceServices rules"

# instance_services_rules <rules.v4>
# Prints the OUTPUT jump into InstanceServices, re-pointed at ufw's ufw-before-output
# chain, then every `-A InstanceServices` rule exactly as the file has it. Prints nothing
# when the file has no such chain (not an Oracle image, or already handed over).
instance_services_rules() {
  [ -r "$1" ] || return 0
  grep -E '^-A OUTPUT .*-j InstanceServices$' "$1" | sed 's/^-A OUTPUT /-A ufw-before-output /'
  grep -E '^-A InstanceServices ' "$1" || true
}

# merge_before_rules <before.rules> <rules text>
# Prints before.rules with the InstanceServices chain declared right after `*filter` and
# the rules right before the first COMMIT, each between marker comments. A previous
# merge's markers are dropped first, so running it again changes nothing. With empty
# rules text it prints the file unchanged (minus any earlier merge).
merge_before_rules() {
  # The rules travel through the environment, not `awk -v`: -v processes backslash
  # escapes, and mawk (Ubuntu's awk) may refuse a newline inside a -v value.
  OP_FW_RULES="$2" awk \
      -v bc="${OP_FW_BEGIN_CHAIN}" -v ec="${OP_FW_END_CHAIN}" \
      -v br="${OP_FW_BEGIN_RULES}" -v er="${OP_FW_END_RULES}" '
    BEGIN { rules = ENVIRON["OP_FW_RULES"] }
    $0 == bc || $0 == br { skip = 1; next }
    $0 == ec || $0 == er { skip = 0; next }
    skip { next }
    {
      if ($0 == "COMMIT" && !committed && rules != "") {
        print br; print rules; print er; committed = 1
      }
      print
      if ($0 == "*filter" && !declared && rules != "") {
        print bc; print ":InstanceServices - [0:0]"; print ec; declared = 1
      }
    }
  ' "$1"
}
