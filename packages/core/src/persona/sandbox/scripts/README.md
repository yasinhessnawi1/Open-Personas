# persona-sandbox host setup scripts

Substrate level egress filter setup for `LocalDockerSandbox`.

| File | Purpose |
|---|---|
| [`setup-sandbox-net.sh`](setup-sandbox-net.sh) | Create the Docker bridge and apply the iptables and ip6tables rules. **Root required.** |
| [`teardown-sandbox-net.sh`](teardown-sandbox-net.sh) | The reverse of setup. Removes the rules (matched by comment) and the bridge. **Root required.** |

## Why these scripts exist, and why they live HERE

These scripts apply the substrate level egress filter on the host where
`LocalDockerSandbox` runs untrusted code. The blocked CIDR list (`BLOCKED_IPV4`,
`BLOCKED_IPV6`) is single source of truth in [`../egress.py`](../egress.py), and
these scripts call the Python entry point
[`apply_egress_rules`](../egress.py) to generate the rules, so the scripts and the
unit tests in [`../../../../tests/unit/sandbox/test_egress.py`](../../../../tests/unit/sandbox/test_egress.py)
can never drift.

### Location choice

The scripts live in `packages/core/src/persona/sandbox/scripts/` alongside
[`../image/`](../image/) (the substrate's Dockerfile and pinned manifest), rather
than in `packages/api/scripts/`:

- **Threat model separation.** The local Docker substrate is in core. The hosted
  E2B path uses E2B's native `update_network()` API for egress filtering, NOT
  these scripts. The api package has no substrate setup of its own.
- **Co-located with the code that uses them.** `LocalDockerSandbox` references
  `SANDBOX_BRIDGE_NAME` and `apply_egress_rules` from
  [`../egress.py`](../egress.py), and the scripts wrap the same entry point.
- **They future-proof the self-Fly Machines exit ramp**, the fallback if E2B
  doesn't pass the five lock gates. The API deploy playbook will reference these
  canonical scripts via the core path.

## Platform: Linux only

**These scripts require a Linux host.** `iptables` and `ip6tables` are Linux
tools, and macOS and Windows don't have them. macOS Docker Desktop runs containers
in a hidden Linux VM (LinuxKit), and that VM has iptables, but it is not directly
reachable from the macOS host.

Running `setup-sandbox-net.sh` on macOS exits with a clear platform limitation
message before creating any host state.

**What this means for §9 acceptance verification:**

- **§9 #5, #6, #8, and #9 verify cleanly on macOS.** Container hardening
  (cap_drop, seccomp, read_only, network=none, resource caps, non-root user) is
  container level config that Docker Desktop applies through its Linux VM
  regardless of host OS. All 20 of the §9 #5/#6/#8/#9 attacks are contained
  empirically on macOS Docker Desktop, verified by the integration suite.
- **§9 #7, metadata endpoint blocking, cannot be empirically verified on macOS dev
  hosts.** The 26 unit tests in
  [`../../../../tests/unit/sandbox/test_egress.py`](../../../../tests/unit/sandbox/test_egress.py)
  pin the rule construction (catalog completeness, DOCKER-USER targeting,
  IPv4-mapped-IPv6 belt and braces, and so on). Live verification requires a Linux
  host: a production deploy, or a Linux dev VM such as OrbStack, multipass, or
  Vagrant.

That is the macOS dev host limitation. It is not a regression and not a hardening
gap. The scripts are designed for the production Linux deploy environment; the
macOS dev host is the test bed for the deploy case, not the deploy itself.

## When to run

### Local dev test bed (Linux contributors only)

Once after cloning the repo on a Linux host, before running the sandbox
integration security suite (`packages/core/tests/integration/sandbox/`):

```bash
sudo packages/core/src/persona/sandbox/scripts/setup-sandbox-net.sh
uv run pytest packages/core/tests/integration/sandbox/ -m integration
```

The §9 #7 metadata endpoint attacks (`aws_imds_v1`, `gcp_metadata_by_name`, and
the rest) need this setup in place. Without it they skip with
`network persona-sandbox-net not found`.

Re-run `setup-sandbox-net.sh` after a Docker daemon restart, since the rules don't
persist by default.

### Hosted deploy (the self-Fly Machines exit ramp)

Once per host at provisioning time. The hosted E2B path, which is the default,
does NOT use these scripts, because E2B has its own `update_network()` API for
substrate level egress. The scripts apply ONLY when LocalDockerSandbox is the
substrate: the CLI, and the self-Fly exit ramp.

## What the rules do

`setup-sandbox-net.sh` runs:

1. `docker network create persona-sandbox-net`, idempotent, skipped if present.
2. `apply_egress_rules(SANDBOX_BRIDGE_NAME)` from
   [`../egress.py`](../egress.py), which applies roughly 26 iptables rules and 28
   ip6tables rules to the `DOCKER-USER` chain. Each rule carries a
   `--comment "persona-sandbox: ..."` so teardown can match and remove cleanly.

The rules DROP traffic from the sandbox bridge to:

- **Cloud metadata:** `169.254.169.254` (AWS, GCP, Azure IMDS) plus the IPv6
  equivalents (`fd00:ec2::254` and friends)
- **RFC-1918 private ranges:** `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`
- **IPv6 link-local and ULA:** `fe80::/10`, `fc00::/7`
- **IPv4-mapped IPv6:** `::ffff:0:0/96`, which defeats the v4-via-v6 bypass
- **Loopback:** `127.0.0.0/8`, `::1/128`
- **Multicast and broadcast:** `224.0.0.0/4`, `ff00::/8`, `255.255.255.255`
- **CGNAT, TEST-NET, and documentation ranges:** RFC 6598, RFC 5737, RFC 6890

The rules apply **before** any persona's `NetworkPolicy.allowed_hosts`. The
substrate level deny list fires regardless of the model's allow list, which is the
SSRF prior-art discipline.

## Manual reverse-out (if a script crashes mid-apply)

```bash
# IPv4: match by our comment prefix; delete by line number descending
sudo iptables -L DOCKER-USER -n --line-numbers | grep 'persona-sandbox'
# Then sudo iptables -D DOCKER-USER <line-number> for each match (descending).

# IPv6: same shape
sudo ip6tables -L DOCKER-USER -n --line-numbers | grep 'persona-sandbox'

# Bridge
sudo docker network rm persona-sandbox-net
```

## Source of truth

- **Blocked CIDR list:** [`../egress.py`](../egress.py), the `BLOCKED_IPV4` and
  `BLOCKED_IPV6` constants.
- **Rule generation:** `build_iptables_rules` and `build_ip6tables_rules` in
  [`../egress.py`](../egress.py).
- **Rule application:** `apply_egress_rules` in [`../egress.py`](../egress.py),
  invoked by this script.
- **Test coverage:** [`../../../../tests/unit/sandbox/test_egress.py`](../../../../tests/unit/sandbox/test_egress.py),
  covering rule construction against 26 pinned invariants.
- **Adversarial verification:** the §9 #7 catalog in
  [`../../../../tests/integration/sandbox/_attacks.py`](../../../../tests/integration/sandbox/_attacks.py)
  exercises this filter against a real sandbox once the bridge exists.
